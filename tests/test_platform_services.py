import plistlib
import shlex
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from openusage_bar import platform_services


class PlatformServicesRenderTests(unittest.TestCase):
    def assert_flag_value(
        self, arguments: list[str], flag: str, expected: str
    ) -> None:
        self.assertIn(flag, arguments)
        index = arguments.index(flag)
        self.assertLess(index + 1, len(arguments))
        self.assertEqual(arguments[index + 1], expected)

    def assert_collector_only(self, arguments: list[str]) -> None:
        self.assertEqual(arguments[:2], ["openusage-bar", "daemon"])
        self.assertFalse(
            any(
                argument.casefold() == "gateway"
                or argument.casefold().startswith("--gateway")
                for argument in arguments
            )
        )

    def test_launchd_plist_is_xml_with_label_and_interval(self):
        rendered = platform_services.launchd_plist(interval=300)

        self.assertIn("<key>Label</key>", rendered)
        self.assertIn("com.lune.openusagebar.collector", rendered)
        self.assertIn("--interval", rendered)
        self.assertIn("300", rendered)

    def test_systemd_unit_contains_exec_and_wanted_by(self):
        unit = platform_services.systemd_unit(interval=300)

        self.assertIn("[Unit]", unit)
        self.assertIn("ExecStart=openusage-bar daemon --interval 300", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_windows_task_xml_contains_command_and_interval(self):
        xml = platform_services.windows_task_xml(interval_minutes=5)

        self.assertIn("openusage-bar", xml)
        self.assertIn("daemon --interval 300", xml)
        self.assertIn("<Task version=", xml)

    def test_launchd_collector_explicitly_uses_unix_local_api(self):
        rendered = platform_services.launchd_plist(
            interval=60,
            api_socket="/state/openusage.sock",
        )
        payload = plistlib.loads(rendered.encode("utf-8"))
        arguments = payload["ProgramArguments"]

        self.assert_collector_only(arguments)
        self.assert_flag_value(arguments, "--api-transport", "unix")
        self.assert_flag_value(arguments, "--api-socket", "/state/openusage.sock")

    def test_systemd_collector_explicitly_uses_unix_local_api(self):
        rendered = platform_services.systemd_unit(
            interval=60,
            api_socket="/state/openusage.sock",
        )
        command = next(
            line.removeprefix("ExecStart=")
            for line in rendered.splitlines()
            if line.startswith("ExecStart=")
        )
        self.assertEqual(
            sum(line.startswith("Exec") for line in rendered.splitlines()),
            1,
        )
        arguments = shlex.split(command)

        self.assert_collector_only(arguments)
        self.assert_flag_value(arguments, "--api-transport", "unix")
        self.assert_flag_value(arguments, "--api-socket", "/state/openusage.sock")

    def test_systemd_socket_path_with_spaces_remains_one_argument(self):
        socket_path = "/state/Usage Hub/openusage.sock"
        rendered = platform_services.systemd_unit(
            interval=60,
            api_socket=socket_path,
        )
        command = next(
            line.removeprefix("ExecStart=")
            for line in rendered.splitlines()
            if line.startswith("ExecStart=")
        )
        arguments = shlex.split(command)

        self.assert_flag_value(arguments, "--api-socket", socket_path)

    def test_systemd_rejects_paths_with_unit_or_expansion_syntax(self):
        unsafe_paths = (
            "/state/openusage.sock\nExecStartPost=/bin/false",
            "/state/openusage.sock\rRestart=always",
            "/state/openusage\x00.sock",
            "/state/%n/openusage.sock",
            "/state/$HOME/openusage.sock",
            "/state/'quoted'/openusage.sock",
            '/state/"quoted"/openusage.sock',
            r"/state/back\\slash/openusage.sock",
        )

        for socket_path in unsafe_paths:
            with self.subTest(socket_path=socket_path):
                with self.assertRaisesRegex(ValueError, "service path is invalid") as raised:
                    platform_services.systemd_unit(api_socket=socket_path)
                self.assertNotIn(socket_path, str(raised.exception))

    def test_windows_collector_explicitly_uses_tokenized_loopback_tcp(self):
        rendered = platform_services.windows_task_xml(interval_minutes=5)
        root = ET.fromstring(rendered)
        command = root.find(".//{*}Command")
        arguments_node = root.find(".//{*}Arguments")

        self.assertIsNotNone(command)
        self.assertIsNotNone(arguments_node)
        self.assertEqual(len(root.findall(".//{*}Exec")), 1)
        self.assertEqual(command.text, "openusage-bar")
        arguments = [
            "openusage-bar",
            *shlex.split(arguments_node.text or "", posix=False),
        ]
        self.assert_collector_only(arguments)
        self.assert_flag_value(arguments, "--api-transport", "tcp")
        self.assert_flag_value(arguments, "--api-port", "17821")
        self.assertIn("--api-token-path", arguments)
        token_path = arguments[arguments.index("--api-token-path") + 1].strip('"')
        self.assertTrue(token_path.replace("\\", "/").endswith("/api.token"))

    def test_windows_native_render_fails_closed_without_current_user_state(self):
        with (
            patch.object(platform_services.sys, "platform", "win32"),
            patch.dict(platform_services.os.environ, {}, clear=True),
        ):
            with self.assertRaisesRegex(
                ValueError, "Windows state directory is unavailable"
            ):
                platform_services.windows_task_xml(interval_minutes=5)

    def test_windows_native_rejects_remote_state_without_default_user_fallback(self):
        private_state = r"\\server\private-profile\AppData\Local"
        with (
            patch.object(platform_services.sys, "platform", "win32"),
            patch.dict(
                platform_services.os.environ,
                {"LOCALAPPDATA": private_state},
                clear=True,
            ),
        ):
            with self.assertRaises(ValueError) as raised:
                platform_services.windows_task_xml(interval_minutes=5)

        self.assertNotIn(private_state, str(raised.exception))
        self.assertNotIn("Users\\Default", str(raised.exception))

    def test_render_current_platform_matches_active_platform(self):
        rendered = platform_services.render_current_platform(interval=300)

        self.assertTrue(rendered)
        if sys.platform == "darwin":
            self.assertIn("<plist", rendered)
        elif sys.platform.startswith("linux"):
            self.assertIn("[Unit]", rendered)
        elif sys.platform == "win32":
            self.assertIn("<Task", rendered)


class PlatformServicesBehaviorTests(unittest.TestCase):
    def test_launchd_plist_expands_custom_paths(self):
        rendered = platform_services.launchd_plist(
            interval=60,
            api_socket="~/.state/api.sock",
            stdout_path="~/out.log",
            stderr_path="~/err.log",
        )

        self.assertIn("60", rendered)
        self.assertIn("/.state/api.sock", rendered)
        self.assertIn("/out.log", rendered)

    def test_systemd_unit_custom_socket(self):
        unit = platform_services.systemd_unit(
            interval=60, api_socket="~/.state/api.sock"
        )

        self.assertIn("--interval 60", unit)
        self.assertIn("/.state/api.sock", unit)

    def test_windows_task_xml_rejects_nonpositive_minutes(self):
        with self.assertRaises(ValueError):
            platform_services.windows_task_xml(interval_minutes=0)

    def test_render_unsupported_platform_raises(self):
        with patch.object(platform_services.sys, "platform", "plan9"):
            with self.assertRaises(RuntimeError):
                platform_services.render_current_platform()

    def test_run_success_and_nonzero_and_exception(self):
        with patch(
            "subprocess.run",
            return_value=__import__("subprocess").CompletedProcess(
                args=[], returncode=0
            ),
        ):
            platform_services._run(["ok"])

        with patch(
            "subprocess.run",
            return_value=__import__("subprocess").CompletedProcess(
                args=[], returncode=2
            ),
        ):
            with self.assertRaises(RuntimeError):
                platform_services._run(["bad"])

        with patch(
            "subprocess.run",
            side_effect=__import__("subprocess").CalledProcessError(1, ["x"]),
        ):
            with self.assertRaises(RuntimeError):
                platform_services._run(["boom"])

    def test_install_service_darwin_writes_plist_and_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            (home / "Library" / "LaunchAgents").mkdir(parents=True)
            calls: list[list[str]] = []

            def fake_run(command: list[str]) -> None:
                calls.append(command)

            with patch.object(platform_services.sys, "platform", "darwin"), patch.object(
                platform_services.Path, "home", lambda: home
            ), patch.object(platform_services, "_run", side_effect=fake_run):
                platform_services.install_service(interval=300)

            plist = (
                home / "Library" / "LaunchAgents"
                / "com.lune.openusagebar.collector.plist"
            )
            self.assertTrue(plist.exists())
            self.assertEqual(calls, [["launchctl", "load", "-w", str(plist)]])

    def test_install_service_linux_writes_unit_and_activates(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir(parents=True)
            calls: list[list[str]] = []

            def fake_run(command: list[str]) -> None:
                calls.append(command)

            with patch.object(platform_services.sys, "platform", "linux"), patch.object(
                platform_services.Path, "home", lambda: home
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.object(platform_services, "_run", side_effect=fake_run):
                platform_services.install_service(interval=60)

            unit = (
                home / ".config" / "systemd" / "user"
                / "openusage-bar.service"
            )
            self.assertTrue(unit.exists())
            self.assertEqual(
                calls,
                [
                    ["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", "--now", "openusage-bar.service"],
                ],
            )

    def test_uninstall_service_linux_removes_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            target = (
                home / ".config" / "systemd" / "user"
                / "openusage-bar.service"
            )
            target.parent.mkdir(parents=True)
            target.write_text("[Unit]", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str]) -> None:
                calls.append(command)

            with patch.object(platform_services.sys, "platform", "linux"), patch.object(
                platform_services.Path, "home", lambda: home
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.object(platform_services, "_run", side_effect=fake_run):
                platform_services.uninstall_service()

            self.assertFalse(target.exists())
            self.assertEqual(
                calls,
                [["systemctl", "--user", "disable", "--now", "openusage-bar.service"]],
            )

    def test_install_service_unsupported_platform_raises(self):
        with patch.object(platform_services.sys, "platform", "plan9"):
            with self.assertRaises(RuntimeError):
                platform_services.install_service()

    def test_windows_task_xml_utf16_and_schtasks_install(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir(parents=True)
            calls: list[list[str]] = []

            def fake_run(command: list[str]) -> None:
                calls.append(command)

            with patch.object(platform_services.sys, "platform", "win32"), patch.dict(
                "os.environ", {"LOCALAPPDATA": str(home)}
            ), patch.object(platform_services, "_run", side_effect=fake_run):
                platform_services.install_service(interval=300)

            xml_path = home / "openusage-bar-task.xml"
            self.assertTrue(xml_path.exists())
            content = xml_path.read_text(encoding="utf-16")
            self.assertIn("daemon --interval 300", content)
            self.assertEqual(
                calls,
                [
                    [
                        "schtasks", "/Create", "/TN",
                        "OpenUsageBarCollector", "/XML", str(xml_path), "/F",
                    ]
                ],
            )
