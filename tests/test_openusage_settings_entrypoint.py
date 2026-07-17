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

    def test_unknown_arguments_fail_without_opening_settings(self):
        with patch.object(sys, "argv", ["openusage_settings.py", "--unexpected"]), patch(
            "openusage_bar.ui.run_provider_settings"
        ) as run:
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path("openusage_settings.py", run_name="__main__")

        self.assertEqual(raised.exception.code, 2)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
