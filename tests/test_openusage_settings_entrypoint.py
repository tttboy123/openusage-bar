import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class SettingsEntrypointTests(unittest.TestCase):
    def test_entrypoint_delegates_to_settings_only_runner(self):
        with patch.object(sys, "argv", ["openusage_settings.py"]), patch(
            "openusage_bar.ui.run_provider_settings"
        ) as run:
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path("openusage_settings.py", run_name="__main__")

        self.assertEqual(raised.exception.code, 0)
        run.assert_called_once_with()

    def test_allowlisted_headless_command_does_not_import_appkit_ui(self):
        with patch.object(sys, "argv", ["openusage_settings.py", "daemon", "--interval", "300"]), patch(
            "openusage_bar.collector_cli.main", return_value=7
        ) as collector, patch.dict(sys.modules, {"openusage_bar.ui": None}):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path("openusage_settings.py", run_name="__main__")

        self.assertEqual(raised.exception.code, 7)
        collector.assert_called_once_with(["daemon", "--interval", "300"])

    def test_costs_command_is_available_from_the_packaged_entrypoint(self):
        arguments = [
            "openusage_settings.py", "costs", "--from", "2026-07-16",
            "--to", "2026-07-16", "--format", "json", "--offline",
        ]
        with patch.object(sys, "argv", arguments), patch(
            "openusage_bar.collector_cli.main", return_value=0
        ) as collector, patch.dict(sys.modules, {"openusage_bar.ui": None}):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path("openusage_settings.py", run_name="__main__")

        self.assertEqual(raised.exception.code, 0)
        collector.assert_called_once_with(arguments[1:])

    def test_dashboard_command_is_available_from_the_packaged_entrypoint(self):
        arguments = ["openusage_settings.py", "dashboard", "--port", "17822"]
        with patch.object(sys, "argv", arguments), patch(
            "openusage_bar.collector_cli.main", return_value=0
        ) as collector, patch.dict(sys.modules, {"openusage_bar.ui": None}):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path("openusage_settings.py", run_name="__main__")

        self.assertEqual(raised.exception.code, 0)
        collector.assert_called_once_with(arguments[1:])

    def test_real_packaging_entry_script_serves_providers_json_offline(self):
        with tempfile.TemporaryDirectory() as home:
            completed = subprocess.run(
                [
                    sys.executable,
                    "openusage_settings.py",
                    "providers",
                    "--format",
                    "json",
                    "--offline",
                ],
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "HOME": home},
                timeout=10,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["schemaVersion"], "1.0")
        self.assertEqual(payload["providers"], [])

    def test_real_packaging_entry_script_serves_snapshot_json_offline(self):
        with tempfile.TemporaryDirectory() as home:
            completed = subprocess.run(
                [
                    sys.executable,
                    "openusage_settings.py",
                    "snapshot",
                    "--format",
                    "json",
                    "--offline",
                ],
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "HOME": home},
                timeout=10,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["schemaVersion"], "1.0")
        self.assertEqual(payload["dataRevision"], 0)
        self.assertIsNone(payload["summary"]["todayTokens"])

    def test_unknown_arguments_fail_without_opening_settings(self):
        with patch.object(sys, "argv", ["openusage_settings.py", "--unexpected"]), patch(
            "openusage_bar.ui.run_provider_settings"
        ) as run:
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path("openusage_settings.py", run_name="__main__")

        self.assertEqual(raised.exception.code, 2)
        run.assert_not_called()

    def test_provider_mutation_is_dispatched_without_opening_appkit(self):
        with patch.object(
            sys, "argv", ["openusage_settings.py", "provider-mutate"]
        ), patch(
            "openusage_bar.provider_commands.run_provider_mutation", return_value=0
        ) as mutate, patch.dict(sys.modules, {"openusage_bar.ui": None}):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path("openusage_settings.py", run_name="__main__")

        self.assertEqual(raised.exception.code, 0)
        mutate.assert_called_once_with(sys.stdin, sys.stdout)

    def test_gateway_account_mutation_is_dispatched_without_opening_appkit(self):
        with patch.object(
            sys, "argv", ["openusage_settings.py", "gateway-account-mutate"]
        ), patch.object(
            sys, "platform", "linux"
        ), patch(
            "openusage_bar.gateway.commands.run_gateway_account_mutation",
            return_value=13,
        ) as mutate, patch.dict(sys.modules, {"openusage_bar.ui": None}):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path("openusage_settings.py", run_name="__main__")

        self.assertEqual(raised.exception.code, 13)
        mutate.assert_called_once_with(sys.stdin, sys.stdout)

    def test_macos_gateway_account_mutation_uses_one_in_process_native_keychain(self):
        native_keychain = object()
        with patch.object(
            sys, "argv", ["openusage_settings.py", "gateway-account-mutate"]
        ), patch.object(
            sys, "platform", "darwin"
        ), patch(
            "openusage_bar.keychain.MacOSKeychain", return_value=native_keychain
        ) as construct, patch(
            "openusage_bar.gateway.commands.run_gateway_account_mutation",
            return_value=13,
        ) as mutate, patch.dict(sys.modules, {"openusage_bar.ui": None}):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path("openusage_settings.py", run_name="__main__")

        self.assertEqual(raised.exception.code, 13)
        construct.assert_called_once_with()
        mutate.assert_called_once_with(
            sys.stdin,
            sys.stdout,
            keychain=native_keychain,
        )

    def test_macos_gateway_account_credential_roundtrip_uses_one_process_identity(self):
        native_keychain = object()
        with patch.object(
            sys,
            "argv",
            [
                "openusage_settings.py",
                "__gateway-account-credential-roundtrip",
                "/private/tmp/openusage-ci.keychain-db",
            ],
        ), patch.object(sys, "platform", "darwin"), patch(
            "openusage_bar.keychain.MacOSKeychain", return_value=native_keychain
        ) as construct, patch(
            "openusage_bar.gateway.commands.run_gateway_account_credential_roundtrip",
            return_value=17,
        ) as roundtrip, patch.dict(sys.modules, {"openusage_bar.ui": None}):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path("openusage_settings.py", run_name="__main__")

        self.assertEqual(raised.exception.code, 17)
        construct.assert_called_once_with(
            keychain_path="/private/tmp/openusage-ci.keychain-db"
        )
        roundtrip.assert_called_once_with(
            sys.stdin,
            sys.stdout,
            keychain=native_keychain,
        )

    def test_gateway_provider_editor_is_dispatched_without_opening_appkit(self):
        with patch.object(
            sys, "argv", ["openusage_settings.py", "gateway-account-editor"]
        ), patch(
            "openusage_bar.gateway.account_editor_tk.run_gateway_account_editor",
            return_value=17,
        ) as editor, patch.dict(sys.modules, {"openusage_bar.ui": None}):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path("openusage_settings.py", run_name="__main__")

        self.assertEqual(raised.exception.code, 17)
        editor.assert_called_once_with(sys.stdin, sys.stdout)

    def test_gateway_provider_editor_self_test_is_dispatched_without_opening_appkit(self):
        with patch.object(
            sys, "argv", ["openusage_settings.py", "gateway-account-editor", "--ui-self-test"]
        ), patch(
            "openusage_bar.gateway.account_editor_tk.run_gateway_account_editor_self_test",
            return_value=0,
        ) as self_test, patch.dict(sys.modules, {"openusage_bar.ui": None}):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path("openusage_settings.py", run_name="__main__")

        self.assertEqual(raised.exception.code, 0)
        self_test.assert_called_once_with(sys.stdout)

    def test_private_keychain_operation_is_dispatched_without_opening_appkit(self):
        with patch.object(
            sys, "argv", ["openusage_settings.py", "__keychain-write"]
        ), patch(
            "openusage_bar.keychain.run_native_keychain_write", return_value=0
        ) as operation, patch.dict(sys.modules, {"openusage_bar.ui": None}):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path("openusage_settings.py", run_name="__main__")

        self.assertEqual(raised.exception.code, 0)
        operation.assert_called_once_with(sys.stdin.buffer, sys.stdout.buffer)

if __name__ == "__main__":
    unittest.main()
