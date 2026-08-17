import builtins
import ctypes
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openusage_bar.bounded_process import BoundedProcessError
import openusage_bar.keychain as keychain_module
from openusage_bar.keychain import (
    BoundedMacOSKeychain,
    HeadlessKeychain,
    InteractiveKeychainAuthorizer,
    KeychainAuthorizationState,
    KeychainError,
    LinuxSecretServiceAPI,
    MacOSKeychain,
    SecurityFrameworkAPI,
    UnsupportedPlatformKeychainError,
    WindowsCredentialManagerAPI,
    default_keychain,
    run_native_keychain_write,
)


@unittest.skipIf(sys.platform == "win32", "macOS bounded keychain behavior")
class KeychainTests(unittest.TestCase):
    def test_security_framework_api_binds_all_operations_to_one_explicit_keychain(self):
        class FakeSecurity:
            errSecSuccess = 0
            errSecItemNotFound = -25300
            kSecValueRef = "value-ref"
            kSecValueData = "value-data"

            def __init__(self) -> None:
                self.item = object()
                self.value: bytes | None = None
                self.events: list[tuple[object, ...]] = []

            def SecKeychainOpen(self, path, output):
                self.events.append(("open", path, output))
                return self.errSecSuccess, "held-keychain"

            def SecKeychainFindGenericPassword(
                self, keychain, service_length, service, account_length, account,
                password_length, password_data, item,
            ):
                self.events.append(("find", keychain, service, account))
                if self.value is None:
                    return self.errSecItemNotFound, 0, None, None
                return self.errSecSuccess, len(self.value), self.value, self.item

            def SecKeychainAddGenericPassword(
                self, keychain, service_length, service, account_length, account,
                password_length, password, item,
            ):
                self.events.append(("add", keychain, service, account, password))
                self.value = bytes(password)
                return self.errSecSuccess, self.item

            def SecItemUpdate(self, query, attributes):
                self.events.append(("update", query, attributes))
                self.value = bytes(attributes[self.kSecValueData])
                return self.errSecSuccess

            def SecKeychainItemDelete(self, item):
                self.events.append(("delete", item))
                self.value = None
                return self.errSecSuccess

        security = FakeSecurity()
        with patch.dict(sys.modules, {"Security": security}):
            api = SecurityFrameworkAPI(
                keychain_path="/private/tmp/openusage-ci.keychain-db"
            )
            query = {
                "service": "com.lune.openusage-menubar",
                "account": "openai.account-ci.gateway-api-key",
            }

            self.assertIsNone(api.get(query))
            api.add(query, b"first")
            self.assertEqual(api.get(query), b"first")
            self.assertTrue(api.update(query, b"second"))
            self.assertEqual(api.get(query), b"second")
            api.delete(query)
            self.assertIsNone(api.get(query))

        self.assertEqual(
            security.events[0],
            ("open", b"/private/tmp/openusage-ci.keychain-db", None),
        )
        self.assertTrue(
            all(
                event[1] == "held-keychain"
                for event in security.events
                if event[0] in {"find", "add"}
            )
        )

    def test_uses_fixed_service_and_provider_account(self):
        api = Mock()
        api.update.return_value = True

        MacOSKeychain(api).set("minimax-main", "secret")

        query, value = api.update.call_args.args
        self.assertEqual(query["service"], "com.lune.openusage-menubar")
        self.assertEqual(query["account"], "minimax-main")
        self.assertEqual(value, b"secret")

    def test_adds_missing_item(self):
        api = Mock()
        api.update.return_value = False

        MacOSKeychain(api).set("demo", "secret")

        api.add.assert_called_once_with(
            {"service": "com.lune.openusage-menubar", "account": "demo"}, b"secret"
        )

    def test_decodes_loaded_value(self):
        api = Mock()
        api.get.return_value = "密钥".encode()

        self.assertEqual(MacOSKeychain(api).get("demo"), "密钥")

    def test_interactive_authorizer_uses_fixed_security_command_without_returning_secret(self):
        runner = Mock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"private-value\n", stderr=b""
            )
        )
        authorizer = InteractiveKeychainAuthorizer(
            runner=runner,
            timeout_seconds=90,
        )

        state = authorizer.authorize(
            service="com.lune.openusage-menubar",
            account="minimax-main",
        )

        self.assertEqual(state, KeychainAuthorizationState.AUTHORIZED)
        self.assertEqual(
            runner.call_args.args[0],
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                "com.lune.openusage-menubar",
                "-a",
                "minimax-main",
                "-w",
            ],
        )
        self.assertFalse(runner.call_args.kwargs["shell"])
        self.assertEqual(
            runner.call_args.kwargs["stdout"],
            subprocess.DEVNULL,
        )
        self.assertEqual(runner.call_args.kwargs["stdout_limit"], 0)
        self.assertEqual(runner.call_args.kwargs["stderr_limit"], 0)
        self.assertEqual(
            runner.call_args.kwargs["stderr"],
            subprocess.DEVNULL,
        )
        self.assertNotIn("private-value", repr(state))

    def test_interactive_authorizer_classifies_missing_denied_and_timeout(self):
        missing_runner = Mock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=44, stdout=b"", stderr=b"not found"
            )
        )
        denied_runner = Mock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=128, stdout=b"", stderr=b"secret detail"
            )
        )
        timeout_runner = Mock(side_effect=BoundedProcessError("timeout"))

        self.assertEqual(
            InteractiveKeychainAuthorizer(runner=missing_runner).authorize(
                service="service", account=None
            ),
            KeychainAuthorizationState.MISSING,
        )
        self.assertEqual(
            InteractiveKeychainAuthorizer(runner=denied_runner).authorize(
                service="service", account=None
            ),
            KeychainAuthorizationState.DENIED,
        )
        self.assertEqual(
            InteractiveKeychainAuthorizer(runner=timeout_runner).authorize(
                service="service", account=None
            ),
            KeychainAuthorizationState.DENIED,
        )

    def test_interactive_authorizer_rejects_unbounded_timeout_and_malformed_identifiers(self):
        with self.assertRaises(ValueError):
            InteractiveKeychainAuthorizer(timeout_seconds=121)
        authorizer = InteractiveKeychainAuthorizer(runner=Mock())

        for service, account in (
            ("", None),
            ("service\nname", None),
            ("service", "account\x00name"),
        ):
            with self.subTest(service=service, account=account):
                with self.assertRaises(ValueError):
                    authorizer.authorize(service=service, account=account)

    def test_native_helper_rejects_reads_and_mutates_allowlisted_credentials(self):
        keychain = Mock()
        keychain.get.return_value = "密钥"
        output = io.BytesIO()

        code = run_native_keychain_write(
            io.BytesIO(json.dumps({
                "version": 1, "action": "get", "account": "step-plan-main",
            }).encode()),
            output,
            keychain=keychain,
        )

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()), {
            "version": 1, "ok": False,
        })
        keychain.get.assert_not_called()

        output = io.BytesIO()
        code = run_native_keychain_write(
            io.BytesIO(json.dumps({
                "version": 1,
                "action": "set",
                "account": "step-plan-main.oasis-token",
                "secret": "rotated-private-token",
            }).encode()),
            output,
            keychain=keychain,
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {
            "version": 1, "ok": True,
        })
        keychain.set.assert_called_once_with(
            "step-plan-main.oasis-token", "rotated-private-token"
        )

        output = io.BytesIO()
        gateway_account = "openai.account-0123456789abcdef0123456789abcdef.gateway-api-key"
        code = run_native_keychain_write(
            io.BytesIO(json.dumps({
                "version": 1,
                "action": "set",
                "account": gateway_account,
                "secret": "sk-private-gateway",
            }).encode()),
            output,
            keychain=keychain,
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {
            "version": 1, "ok": True,
        })
        keychain.set.assert_called_with(
            gateway_account, "sk-private-gateway"
        )

        output = io.BytesIO()
        code = run_native_keychain_write(
            io.BytesIO(json.dumps({
                "version": 1,
                "action": "delete",
                "account": gateway_account,
            }).encode()),
            output,
            keychain=keychain,
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {
            "version": 1, "ok": True,
        })
        keychain.delete.assert_called_once_with(gateway_account)

        output = io.BytesIO()
        code = run_native_keychain_write(
            io.BytesIO(json.dumps({
                "version": 1,
                "action": "set",
                "account": "minimax-main",
                "secret": "must-not-be-written",
            }).encode()),
            output,
            keychain=keychain,
        )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()), {
            "version": 1, "ok": False,
        })
        self.assertEqual(keychain.set.call_count, 2)

    def test_native_helper_rejects_gateway_default_and_malformed_mutable_accounts(self):
        keychain = Mock()

        for account in (
            "openai.gateway-api-key",
            "openai.work.gateway-api-key",
            "ollama.account-0123456789abcdef0123456789abcdef.gateway-api-key",
            "unknown.account-0123456789abcdef0123456789abcdef.gateway-api-key",
            "openai.account-0123456789abcdef0123456789abcdeg.gateway-api-key",
        ):
            with self.subTest(account=account):
                output = io.BytesIO()
                code = run_native_keychain_write(
                    io.BytesIO(json.dumps({
                        "version": 1,
                        "action": "set",
                        "account": account,
                        "secret": "must-not-be-written",
                    }).encode()),
                    output,
                    keychain=keychain,
                )

                self.assertEqual(code, 1)
                self.assertEqual(json.loads(output.getvalue()), {
                    "version": 1, "ok": False,
                })

        keychain.set.assert_not_called()

    def test_bounded_keychain_reads_via_reader_and_privately_mutates_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "keychain-helper.py"
            capture = Path(directory) / "capture.json"
            helper.write_text(
                "import json,sys,pathlib\n"
                "request=json.loads(sys.stdin.buffer.read())\n"
                f"path=pathlib.Path({str(capture)!r})\n"
                "items=json.loads(path.read_text()) if path.exists() else []\n"
                "items.append({'argv':sys.argv[1:],'request':request})\n"
                "path.write_text(json.dumps(items))\n"
                "response={'version':1,'ok':True}\n"
                "sys.stdout.write(json.dumps(response))\n",
                encoding="utf-8",
            )
            reader = Mock()
            reader.get.return_value = "stored-value"
            keychain = BoundedMacOSKeychain(
                helper_command=(sys.executable, str(helper)),
                timeout_seconds=2,
                reader=reader,
            )

            self.assertEqual(keychain.get("step-plan-main"), "stored-value")
            reader.get.assert_called_once_with("step-plan-main")
            self.assertFalse(capture.exists())
            secret = "rotated-private-token"
            keychain.set("step-plan-main.oasis-token", secret)
            gateway_secret = "sk-private-gateway"
            gateway_account = (
                "openai.account-0123456789abcdef0123456789abcdef.gateway-api-key"
            )
            keychain.set(gateway_account, gateway_secret)
            keychain.delete(gateway_account)
            captured = json.loads(capture.read_text(encoding="utf-8"))

        self.assertEqual([item["argv"] for item in captured], [
            ["__keychain-write"],
            ["__keychain-write"],
            ["__keychain-write"],
        ])
        self.assertEqual(captured[0]["request"]["secret"], secret)
        self.assertEqual(captured[1]["request"]["secret"], gateway_secret)
        self.assertEqual(captured[2]["request"], {
            "version": 1,
            "action": "delete",
            "account": gateway_account,
        })
        self.assertNotIn(secret, " ".join(keychain.command))
        self.assertNotIn(gateway_secret, " ".join(keychain.command))

    def test_bounded_keychain_timeout_is_sanitized_and_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "keychain-helper.py"
            helper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            keychain = BoundedMacOSKeychain(
                helper_command=(sys.executable, str(helper)),
                timeout_seconds=1,
                reader=Mock(get=Mock(return_value=None)),
            )

            started = time.monotonic()
            self.assertIsNone(keychain.get("step-plan-main"))
            self.assertLess(time.monotonic() - started, 1)
            with self.assertRaises(KeychainError) as raised:
                keychain.set("step-plan-main.oasis-token", "never-in-error")
            self.assertNotIn("never-in-error", str(raised.exception))

    def test_bounded_keychain_rejects_malformed_gateway_write_accounts_before_helper(self):
        helper = Mock()
        keychain = BoundedMacOSKeychain(
            helper_command=(sys.executable, "-c"),
            reader=Mock(get=Mock(return_value=None)),
        )
        keychain._request = helper

        for account in (
            "openai.gateway-api-key",
            "openai.work.gateway-api-key",
            "ollama.account-0123456789abcdef0123456789abcdef.gateway-api-key",
        ):
            with self.subTest(account=account):
                with self.assertRaises(KeychainError):
                    keychain.set(account, "private-secret")
                with self.assertRaises(KeychainError):
                    keychain.delete(account)

        helper.assert_not_called()


class CrossPlatformKeychainTests(unittest.TestCase):
    def test_linux_default_keychain_construction_does_not_import_secretstorage(self):
        real_import = builtins.__import__
        secretstorage_imports = []

        def guarded_import(name, *args, **kwargs):
            if name == "secretstorage":
                secretstorage_imports.append(name)
                raise ImportError("secretstorage must remain lazy")
            return real_import(name, *args, **kwargs)

        with (
            patch("openusage_bar.keychain.sys.platform", "linux"),
            patch("builtins.__import__", side_effect=guarded_import),
        ):
            keychain = default_keychain()

        self.assertIsInstance(keychain, HeadlessKeychain)
        self.assertEqual(secretstorage_imports, [])

    def test_linux_credential_read_fails_closed_when_secretstorage_is_missing(self):
        real_import = builtins.__import__
        private_path = "/Users/private/.local/lib/python/secretstorage.py"

        def unavailable_import(name, *args, **kwargs):
            if name == "secretstorage":
                raise ImportError(f"missing optional module at {private_path}")
            return real_import(name, *args, **kwargs)

        with (
            patch("openusage_bar.keychain.sys.platform", "linux"),
            patch("builtins.__import__", side_effect=unavailable_import),
        ):
            keychain = default_keychain()
            with self.assertRaises(KeychainError) as raised:
                keychain.get("provider-main")

        public_error = str(raised.exception)
        self.assertEqual(
            public_error,
            "Linux Secret Service requires the optional 'secretstorage' dependency",
        )
        self.assertNotIn("ImportError", public_error)
        self.assertNotIn(private_path, public_error)

    def test_headless_keychain_maps_account_to_fixed_service(self):
        api = Mock()
        api.get.return_value = b"secret"

        self.assertEqual(HeadlessKeychain(api).get("minimax-main"), "secret")
        api.get.assert_called_once_with(
            {"service": "com.lune.openusage-menubar", "account": "minimax-main"}
        )

    def test_headless_keychain_adds_when_update_reports_missing(self):
        api = Mock()
        api.update.return_value = False

        HeadlessKeychain(api).set("demo", "secret")

        api.add.assert_called_once_with(
            {"service": "com.lune.openusage-menubar", "account": "demo"}, b"secret"
        )

    def test_headless_keychain_rejects_invalid_utf8(self):
        api = Mock()
        api.get.return_value = b"\xff\xfe"

        with self.assertRaises(KeychainError):
            HeadlessKeychain(api).get("demo")

    @unittest.skipIf(sys.platform == "win32", "Windows backend is native on Windows")
    def test_windows_backend_requires_windows(self):
        with self.assertRaises(UnsupportedPlatformKeychainError):
            WindowsCredentialManagerAPI()

    def test_windows_backend_maps_service_account_and_persists(self):
        native = Mock()
        native.read.return_value = None
        api = WindowsCredentialManagerAPI(native=native)
        query = {"service": "com.lune.openusage-menubar", "account": "demo"}

        self.assertIsNone(api.get(query))
        native.read.assert_called_once_with("com.lune.openusage-menubar\\demo")
        self.assertFalse(api.update(query, b"value"))
        native.write.assert_called_once_with("com.lune.openusage-menubar\\demo", b"value")

        api.add(query, b"value")
        api.delete(query)
        native.delete.assert_called_once_with("com.lune.openusage-menubar\\demo")

    def test_windows_native_read_uses_its_bound_ctypes_runtime(self):
        native_type = keychain_module._WinCredentialNative
        native = native_type.__new__(native_type)
        native._ctypes = Mock(wraps=ctypes)
        native._cred_read = Mock(return_value=False)
        native._get_last_error = Mock(return_value=native.ERROR_NOT_FOUND)
        native._cred_free = Mock()

        self.assertIsNone(native.read("com.lune.openusage-menubar\\missing"))
        native._ctypes.byref.assert_called_once()

    @unittest.skipIf(sys.platform.startswith("linux"), "Linux backend is native on Linux")
    def test_linux_backend_requires_linux(self):
        with self.assertRaises(UnsupportedPlatformKeychainError):
            LinuxSecretServiceAPI()

    def test_linux_backend_maps_attributes_and_secret(self):
        backend = Mock()
        backend.get.return_value = b"secret"
        api = LinuxSecretServiceAPI(backend=backend)
        query = {"service": "com.lune.openusage-menubar", "account": "demo"}

        self.assertEqual(api.get(query), b"secret")
        self.assertTrue(api.update(query, b"new"))
        backend.set.assert_called_once_with(query, b"new")
        api.add(query, b"new")
        api.delete(query)
        backend.delete.assert_called_once_with(query)


class BoundedKeychainCacheTests(unittest.TestCase):
    def _keychain(self, reader):
        return BoundedMacOSKeychain(
            helper_command=(sys.executable, "-c"),
            timeout_seconds=2,
            reader=reader,
        )

    def test_successful_read_is_cached_until_set_or_delete(self):
        reader = Mock()
        reader.get.return_value = "stored-value"
        keychain = self._keychain(reader)

        self.assertEqual(keychain.get("demo"), "stored-value")
        self.assertEqual(keychain.get("demo"), "stored-value")
        reader.get.assert_called_once_with("demo")

        keychain._request = Mock(return_value={"version": 1, "ok": True})
        keychain.set("step-plan-main.oasis-token", "rotated-secret")
        self.assertEqual(keychain.get("step-plan-main.oasis-token"), "rotated-secret")
        reader.get.assert_called_once_with("demo")

        keychain.delete("step-plan-main.oasis-token")
        reader.get.return_value = None
        self.assertIsNone(keychain.get("step-plan-main.oasis-token"))
        self.assertEqual(reader.get.call_count, 2)

    def test_unavailable_read_is_cached_until_negative_ttl_expires(self):
        reader = Mock()
        reader.get.return_value = None
        keychain = self._keychain(reader)

        self.assertIsNone(keychain.get("demo"))
        self.assertIsNone(keychain.get("demo"))
        reader.get.assert_called_once_with("demo")

        keychain._negative_cache_seconds = -1
        self.assertIsNone(keychain.get("demo"))
        self.assertEqual(reader.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
