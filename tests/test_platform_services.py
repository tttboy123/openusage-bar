import plistlib
import shlex
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from openusage_bar import platform_services
from openusage_bar.lifecycle_state import LifecycleStatePaths


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

    def test_windows_collector_task_starts_at_logon_and_when_available(self):
        xml = platform_services.windows_task_xml(interval_minutes=5)

        self.assertIn("<LogonTrigger>", xml)
        self.assertIn("<StartWhenAvailable>true</StartWhenAvailable>", xml)
        self.assertNotIn("<CalendarTrigger>", xml)

    def test_native_windows_task_ignores_hostile_local_app_data_environment(self):
        paths = LifecycleStatePaths(
            platform="win32",
            home=Path(r"C:\Users\Authoritative"),
            local_app_data=Path(r"C:\Users\Authoritative\AppData\Local"),
        )
        with patch.object(platform_services.sys, "platform", "win32"), patch.object(
            platform_services.os, "name", "nt"
        ), patch.dict(
            "os.environ", {"LOCALAPPDATA": r"D:\Hostile\Local"}
        ), patch(
            "openusage_bar.lifecycle_state.LifecycleStatePaths.for_current_user",
            return_value=paths,
        ):
            xml = platform_services.windows_task_xml(interval_minutes=5)

        self.assertIn(
            r"C:\Users\Authoritative\AppData\Local\openusage-bar\api.token",
            xml,
        )
        self.assertNotIn(r"D:\Hostile", xml)

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

    def test_systemd_uses_one_validated_packaged_collector_command(self):
        command_path = "/opt/Usage Hub/resources/collector/openusage-collector"
        rendered = platform_services.systemd_unit(
            interval=60,
            api_socket="/state/openusage.sock",
            command=command_path,
        )
        command = next(
            line.removeprefix("ExecStart=")
            for line in rendered.splitlines()
            if line.startswith("ExecStart=")
        )
        arguments = shlex.split(command)

        self.assertEqual(arguments[0], command_path)
        self.assertEqual(arguments[1], "daemon")
        self.assertEqual(arguments.count(command_path), 1)

    def test_service_renderers_reject_untrusted_collector_commands(self):
        for command in (
            "relative/openusage-collector",
            "/opt/openusage-collector\nExecStart=/bin/false",
            "/opt/openusage-collector\x00private",
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(
                    ValueError, "service command is invalid"
                ) as raised:
                    platform_services.systemd_unit(command=command)
                self.assertNotIn(command, str(raised.exception))

    def test_windows_task_uses_validated_packaged_collector_command(self):
        command_path = r"C:\Program Files\UsageHub\resources\collector\openusage-collector.exe"
        rendered = platform_services.windows_task_xml(
            interval_minutes=5,
            command=command_path,
        )
        root = ET.fromstring(rendered)
        command = root.find(".//{*}Command")

        self.assertIsNotNone(command)
        self.assertEqual(command.text, command_path)

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
    def test_linux_service_registration_probe_uses_systemd_user_manager(self):
        completed = platform_services.subprocess.CompletedProcess(args=[], returncode=0)
        with patch.object(
            platform_services.shutil, "which", return_value="/usr/bin/systemctl"
        ), patch.object(
            platform_services.subprocess, "run", return_value=completed
        ) as run:
            registered = platform_services.service_is_registered(platform="linux")

        self.assertTrue(registered)
        self.assertEqual(
            run.call_args.args[0],
            ["systemctl", "--user", "is-active", "--quiet", "openusage-bar.service"],
        )

    def test_linux_inactive_service_still_blocks_when_unit_is_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            unit = home / ".config" / "systemd" / "user" / "openusage-bar.service"
            unit.parent.mkdir(parents=True)
            unit.write_text("[Unit]", encoding="utf-8")
            with patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.object(platform_services, "_service_status", return_value=3):
                registered = platform_services.service_is_registered(
                    platform="linux",
                    home=home,
                )

        self.assertTrue(registered)

    def test_service_registration_probe_distinguishes_absent_from_manager_failure(self):
        cases = (
            ("linux", 3, False),
            ("linux", 1, platform_services.ServiceCommandError),
            ("win32", 1, False),
            ("win32", 5, platform_services.ServiceCommandError),
        )
        for active_platform, returncode, expected in cases:
            with self.subTest(platform=active_platform, returncode=returncode), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.object(
                platform_services,
                "_service_status",
                return_value=returncode,
            ), patch.object(
                platform_services,
                "_windows_task_definition_path",
                return_value=Path("/openusage-task-does-not-exist"),
            ):
                if isinstance(expected, type) and issubclass(expected, Exception):
                    with self.assertRaises(expected):
                        platform_services.service_is_registered(platform=active_platform)
                else:
                    self.assertIs(
                        platform_services.service_is_registered(platform=active_platform),
                        expected,
                    )

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

    def test_darwin_install_rejects_symlinked_plist_without_touching_foreign(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            plist = agents / "com.lune.openusagebar.collector.plist"
            foreign = root / "PRIVATE_FOREIGN.plist"
            foreign.write_text("preserve", encoding="utf-8")
            plist.symlink_to(foreign)

            with patch.object(platform_services.sys, "platform", "darwin"), patch.object(
                platform_services.Path, "home", lambda: home
            ), patch.object(platform_services, "_run") as run:
                with self.assertRaises(RuntimeError):
                    platform_services.install_service(interval=300)

            self.assertEqual(foreign.read_text(encoding="utf-8"), "preserve")
            run.assert_not_called()

    def test_install_service_linux_writes_unit_and_activates(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir(parents=True)
            calls: list[list[str]] = []

            def fake_run(command: list[str]) -> None:
                calls.append(command)

            with patch.object(platform_services.sys, "platform", "linux"), patch.object(
                platform_services, "_linux_current_home", return_value=home
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

    def test_linux_install_ignores_hostile_home_for_unit_and_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            authoritative_home = Path(directory) / "authoritative"
            authoritative_home.mkdir()
            hostile_home = Path(directory) / "hostile"
            paths = LifecycleStatePaths(
                platform="linux",
                home=authoritative_home,
            )
            with patch.object(platform_services.sys, "platform", "linux"), patch.dict(
                "os.environ", {"HOME": str(hostile_home)}
            ), patch(
                "openusage_bar.lifecycle_state.LifecycleStatePaths.for_current_user",
                return_value=paths,
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.object(platform_services, "_run"):
                platform_services.install_service(interval=60)

            unit = (
                authoritative_home
                / ".config"
                / "systemd"
                / "user"
                / "openusage-bar.service"
            )
            rendered = unit.read_text(encoding="utf-8")
            self.assertIn(
                str(
                    authoritative_home
                    / ".local"
                    / "state"
                    / "openusage-bar"
                    / "openusage.sock"
                ),
                rendered,
            )
            self.assertNotIn(str(hostile_home), rendered)

    def test_linux_install_rejects_symlinked_unit_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            unit = home / ".config" / "systemd" / "user" / "openusage-bar.service"
            unit.parent.mkdir(parents=True)
            foreign = Path(directory) / "foreign.service"
            foreign.write_text("preserve", encoding="utf-8")
            unit.symlink_to(foreign)
            with patch.object(platform_services.sys, "platform", "linux"), patch.object(
                platform_services, "_linux_current_home", return_value=home
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.object(platform_services, "_run") as run:
                with self.assertRaises(RuntimeError):
                    platform_services.install_service(interval=60)

            self.assertEqual(foreign.read_text(encoding="utf-8"), "preserve")
            run.assert_not_called()

    def test_linux_install_rejects_symlinked_config_before_creating_foreign_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            foreign = root / "foreign-config"
            foreign.mkdir()
            sentinel = foreign / "sentinel"
            sentinel.write_text("preserve", encoding="utf-8")
            (home / ".config").symlink_to(foreign, target_is_directory=True)

            with patch.object(platform_services.sys, "platform", "linux"), patch.object(
                platform_services, "_linux_current_home", return_value=home
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.object(platform_services, "_run") as run:
                with self.assertRaises(RuntimeError) as raised:
                    platform_services.install_service(interval=60)

            self.assertEqual(
                sorted(path.relative_to(foreign) for path in foreign.rglob("*")),
                [Path("sentinel")],
            )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
            self.assertNotIn(str(root), str(raised.exception))
            run.assert_not_called()

    def test_linux_parent_creation_is_bound_from_trusted_root_during_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            (home / ".config").mkdir(parents=True)
            original_home = root / "original-home"
            foreign_home = root / "PRIVATE_FOREIGN_HOME"
            foreign_config = foreign_home / ".config"
            foreign_config.mkdir(parents=True)
            sentinel = foreign_config / "sentinel"
            sentinel.write_text("preserve", encoding="utf-8")
            original_open = platform_services.os.open
            swapped = False

            def swap_before_first_absolute_open(path, flags, *args, **kwargs):
                nonlocal swapped
                candidate = Path(path) if isinstance(path, (str, Path)) else None
                if not swapped and candidate is not None and candidate.is_absolute():
                    home.rename(original_home)
                    home.symlink_to(foreign_home, target_is_directory=True)
                    swapped = True
                return original_open(path, flags, *args, **kwargs)

            with patch.object(platform_services.sys, "platform", "linux"), patch.object(
                platform_services, "_linux_current_home", return_value=home
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.object(
                platform_services.os, "open", side_effect=swap_before_first_absolute_open
            ), patch.object(platform_services, "_run") as run:
                with self.assertRaises(RuntimeError):
                    platform_services.install_service(interval=60)

            self.assertTrue(swapped)
            self.assertEqual(
                sorted(path.relative_to(foreign_config) for path in foreign_config.rglob("*")),
                [Path("sentinel")],
            )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
            run.assert_not_called()

    def test_linux_plugin_install_rejects_symlinked_config_without_touching_foreign(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            foreign = root / "PRIVATE_FOREIGN_CONFIG"
            foreign.mkdir()
            sentinel = foreign / "sentinel"
            sentinel.write_text("preserve", encoding="utf-8")
            (home / ".config").symlink_to(foreign, target_is_directory=True)
            collector = root / "package" / "collector"
            collector.parent.mkdir()
            collector.write_bytes(b"collector")
            with patch.object(
                platform_services.sys, "platform", "linux"
            ), patch.object(
                platform_services, "_linux_current_home", return_value=home
            ), patch.object(
                platform_services, "_plugin_command", return_value=str(collector)
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.object(platform_services, "_run") as run:
                with self.assertRaises(RuntimeError):
                    platform_services.install_plugin_service()

            self.assertEqual(
                sorted(path.relative_to(foreign) for path in foreign.rglob("*")),
                [Path("sentinel")],
            )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
            run.assert_not_called()

    def test_linux_plugin_install_activation_failure_removes_owned_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            collector = root / "package" / "collector"
            collector.parent.mkdir()
            collector.write_bytes(b"collector")

            def fail_activation(command: list[str]) -> None:
                if "enable" in command:
                    raise RuntimeError("PRIVATE_MANAGER_FAILURE")

            with patch.object(
                platform_services.sys, "platform", "linux"
            ), patch.object(
                platform_services, "_linux_current_home", return_value=home
            ), patch.object(
                platform_services, "_plugin_command", return_value=str(collector)
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.object(
                platform_services, "_run", side_effect=fail_activation
            ):
                with self.assertRaises(RuntimeError):
                    platform_services.install_plugin_service()

            self.assertFalse(
                (home / ".config" / "systemd" / "user" / platform_services.PLUGIN_SYSTEMD_UNIT_NAME).exists()
            )

    def test_linux_plugin_uninstall_rejects_symlinked_ancestor_without_unlinking_foreign(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            foreign = root / "PRIVATE_FOREIGN_CONFIG"
            unit = foreign / "systemd" / "user" / platform_services.PLUGIN_SYSTEMD_UNIT_NAME
            unit.parent.mkdir(parents=True)
            unit.write_text("preserve", encoding="utf-8")
            (home / ".config").symlink_to(foreign, target_is_directory=True)
            with patch.object(
                platform_services.sys, "platform", "linux"
            ), patch.object(
                platform_services, "_linux_current_home", return_value=home
            ), patch.object(
                platform_services.Path, "home", return_value=home
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.object(platform_services, "_run") as run:
                with self.assertRaises(RuntimeError):
                    platform_services.uninstall_plugin_service()

            self.assertEqual(unit.read_text(encoding="utf-8"), "preserve")
            run.assert_not_called()

    def test_linux_plugin_uninstall_manager_failure_preserves_owned_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            unit = home / ".config" / "systemd" / "user" / platform_services.PLUGIN_SYSTEMD_UNIT_NAME
            unit.parent.mkdir(parents=True)
            unit.write_text("owned", encoding="utf-8")
            with patch.object(
                platform_services.sys, "platform", "linux"
            ), patch.object(
                platform_services, "_linux_current_home", return_value=home
            ), patch.object(
                platform_services.Path, "home", return_value=home
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.object(
                platform_services, "_run", side_effect=RuntimeError("PRIVATE_MANAGER_FAILURE")
            ):
                with self.assertRaises(RuntimeError):
                    platform_services.uninstall_plugin_service()

            self.assertEqual(unit.read_text(encoding="utf-8"), "owned")

    def test_darwin_plugin_uninstall_rejects_symlinked_definition_before_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            foreign = root / "PRIVATE_FOREIGN.plist"
            foreign.write_text("preserve", encoding="utf-8")
            target = agents / f"{platform_services.PLUGIN_LABEL}.plist"
            target.symlink_to(foreign)
            with patch.object(
                platform_services.sys, "platform", "darwin"
            ), patch.object(
                platform_services.Path, "home", return_value=home
            ), patch.object(platform_services, "_run") as run:
                with self.assertRaises(RuntimeError):
                    platform_services.uninstall_plugin_service()

            self.assertTrue(target.is_symlink())
            self.assertEqual(foreign.read_text(encoding="utf-8"), "preserve")
            run.assert_not_called()

    def test_linux_service_rename_cannot_be_redirected_by_parent_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            user_units = home / ".config" / "systemd" / "user"
            user_units.mkdir(parents=True)
            original_units = root / "original-user-units"
            foreign_units = root / "PRIVATE_FOREIGN" / "user"
            foreign_units.mkdir(parents=True)
            foreign_unit = foreign_units / "openusage-bar.service"
            foreign_unit.write_text("preserve", encoding="utf-8")
            original_replace = platform_services.os.replace
            swapped = False

            def swap_at_replace(source, target, *args, **kwargs):
                nonlocal swapped
                user_units.rename(original_units)
                user_units.symlink_to(foreign_units, target_is_directory=True)
                Path(source).with_name(Path(source).name).parent.mkdir(
                    parents=True, exist_ok=True
                )
                (foreign_units / Path(source).name).write_text(
                    "attacker-temp", encoding="utf-8"
                )
                swapped = True
                return original_replace(source, target, *args, **kwargs)

            with patch.object(platform_services.sys, "platform", "linux"), patch.object(
                platform_services, "_linux_current_home", return_value=home
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.object(
                platform_services.os, "replace", side_effect=swap_at_replace
            ), patch.object(platform_services, "_run"):
                try:
                    platform_services.install_service(interval=60)
                except RuntimeError:
                    pass

            self.assertTrue(swapped)
            self.assertEqual(foreign_unit.read_text(encoding="utf-8"), "preserve")

    def test_linux_service_removal_rejects_ancestor_swap_without_unlinking_foreign_unit(self):
        for action in ("install_rollback", "uninstall"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home = root / "home"
                user_units = home / ".config" / "systemd" / "user"
                user_units.mkdir(parents=True)
                unit = user_units / "openusage-bar.service"
                if action == "uninstall":
                    unit.write_text("[Unit]\n", encoding="utf-8")
                foreign_units = root / "PRIVATE_FOREIGN" / "user"
                foreign_units.mkdir(parents=True)
                foreign_unit = foreign_units / "openusage-bar.service"
                foreign_unit.write_text("preserve", encoding="utf-8")
                original_units = root / "original-user-units"
                swapped = False

                def swap_after_manager_call(command: list[str]) -> None:
                    nonlocal swapped
                    should_swap = (
                        action == "install_rollback"
                        and command[:3] == ["systemctl", "--user", "daemon-reload"]
                    ) or (
                        action == "uninstall"
                        and command[:3] == ["systemctl", "--user", "disable"]
                    )
                    if should_swap and not swapped:
                        user_units.rename(original_units)
                        user_units.symlink_to(foreign_units, target_is_directory=True)
                        swapped = True
                        if action == "install_rollback":
                            raise platform_services.ServiceCommandError(returncode=1)

                with patch.object(
                    platform_services.sys, "platform", "linux"
                ), patch.object(
                    platform_services, "_linux_current_home", return_value=home
                ), patch.object(
                    platform_services.shutil,
                    "which",
                    return_value="/usr/bin/systemctl",
                ), patch.object(
                    platform_services, "_run", side_effect=swap_after_manager_call
                ):
                    with self.assertRaises(RuntimeError) as raised:
                        if action == "install_rollback":
                            platform_services.install_service(interval=60)
                        else:
                            platform_services.uninstall_service()

                self.assertTrue(swapped)
                self.assertTrue(
                    foreign_unit.exists(),
                    "service removal must not unlink a foreign unit after an ancestor swap",
                )
                self.assertEqual(
                    foreign_unit.read_text(encoding="utf-8"), "preserve"
                )
                self.assertNotIn(str(root), str(raised.exception))

    def test_linux_activation_failure_rolls_back_unit_and_reloads_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            calls: list[list[str]] = []

            def fake_run(command: list[str]) -> None:
                calls.append(command)
                if command[:3] == ["systemctl", "--user", "enable"]:
                    raise platform_services.ServiceCommandError(returncode=1)

            with patch.object(platform_services.sys, "platform", "linux"), patch.object(
                platform_services, "_linux_current_home", return_value=home
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.object(platform_services, "_run", side_effect=fake_run):
                with self.assertRaises(platform_services.ServiceCommandError):
                    platform_services.install_service(interval=60)

            unit = home / ".config" / "systemd" / "user" / "openusage-bar.service"
            self.assertFalse(unit.exists())
            self.assertEqual(
                calls,
                [
                    ["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable", "--now", "openusage-bar.service"],
                    ["systemctl", "--user", "disable", "--now", "openusage-bar.service"],
                    ["systemctl", "--user", "daemon-reload"],
                ],
            )

    def test_install_service_linux_binds_the_packaged_collector_path(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir(parents=True)
            command = "/opt/Usage Hub/resources/collector/openusage-collector"
            with patch.object(platform_services.sys, "platform", "linux"), patch.object(
                platform_services, "_linux_current_home", return_value=home
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.object(platform_services, "_run"):
                platform_services.install_service(interval=60, command=command)

            unit = (
                home / ".config" / "systemd" / "user"
                / "openusage-bar.service"
            ).read_text(encoding="utf-8")
            self.assertIn(
                'ExecStart="/opt/Usage Hub/resources/collector/openusage-collector" daemon',
                unit,
            )

    def test_linux_install_fails_without_systemctl_and_leaves_no_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            unit = home / ".config" / "systemd" / "user" / "openusage-bar.service"
            with patch.object(platform_services.sys, "platform", "linux"), patch.object(
                platform_services, "_linux_current_home", return_value=home
            ), patch.object(platform_services.shutil, "which", return_value=None):
                with self.assertRaises(RuntimeError):
                    platform_services.install_service(interval=60)

            self.assertFalse(unit.exists())

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
                platform_services, "_linux_current_home", return_value=home
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.object(platform_services, "_run", side_effect=fake_run):
                platform_services.uninstall_service()

            self.assertFalse(target.exists())
            self.assertEqual(
                calls,
                [
                    ["systemctl", "--user", "disable", "--now", "openusage-bar.service"],
                    ["systemctl", "--user", "daemon-reload"],
                ],
            )

    def test_linux_uninstall_fails_without_systemctl_and_preserves_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            target = home / ".config" / "systemd" / "user" / "openusage-bar.service"
            target.parent.mkdir(parents=True)
            target.write_text("[Unit]", encoding="utf-8")
            with patch.object(platform_services.sys, "platform", "linux"), patch.object(
                platform_services, "_linux_current_home", return_value=home
            ), patch.object(platform_services.shutil, "which", return_value=None):
                with self.assertRaises(RuntimeError):
                    platform_services.uninstall_service()

            self.assertTrue(target.exists())

    def test_linux_uninstall_rejects_broken_unit_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            target = home / ".config" / "systemd" / "user" / "openusage-bar.service"
            target.parent.mkdir(parents=True)
            target.symlink_to(Path(directory) / "missing-unit")
            with patch.object(platform_services.sys, "platform", "linux"), patch.object(
                platform_services, "_linux_current_home", return_value=home
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.object(platform_services, "_run") as run:
                with self.assertRaises(RuntimeError):
                    platform_services.uninstall_service()

            self.assertTrue(target.is_symlink())
            run.assert_not_called()

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
                    ],
                    [
                        "schtasks", "/Run", "/TN", "OpenUsageBarCollector",
                    ],
                ],
            )

    def test_install_service_windows_binds_the_packaged_collector_path(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir(parents=True)
            command = r"C:\Program Files\UsageHub\resources\collector\openusage-collector.exe"
            with patch.object(platform_services.sys, "platform", "win32"), patch.dict(
                "os.environ", {"LOCALAPPDATA": str(home)}
            ), patch.object(platform_services, "_run"):
                platform_services.install_service(
                    interval=300,
                    command=command,
                )

            content = (home / "openusage-bar-task.xml").read_text(
                encoding="utf-16"
            )
            self.assertIn(f"<Command>{command}</Command>", content)

    def test_windows_run_failure_rolls_back_task_and_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            local_app_data = Path(directory) / "Local"
            local_app_data.mkdir(parents=True)
            calls: list[list[str]] = []

            def fake_run(command: list[str]) -> None:
                calls.append(command)
                if command[1] == "/Run":
                    raise RuntimeError("activation failed")

            with patch.object(platform_services.sys, "platform", "win32"), patch.dict(
                "os.environ", {"LOCALAPPDATA": str(local_app_data)}
            ), patch.object(platform_services, "_run", side_effect=fake_run):
                with self.assertRaises(RuntimeError):
                    platform_services.install_service(interval=300)

            self.assertEqual(
                [command[1] for command in calls],
                ["/Create", "/Run", "/Delete"],
            )
            self.assertFalse((local_app_data / "openusage-bar-task.xml").exists())

    def test_native_windows_install_writes_definition_to_known_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile"
            local_app_data = profile / "AppData" / "Local"
            local_app_data.mkdir(parents=True)
            hostile = Path(directory) / "hostile"
            hostile.mkdir()
            paths = LifecycleStatePaths(
                platform="win32",
                home=profile,
                local_app_data=local_app_data,
            )
            with patch.object(platform_services.sys, "platform", "win32"), patch.object(
                platform_services.os, "name", "nt"
            ), patch.dict(
                "os.environ", {"LOCALAPPDATA": str(hostile)}
            ), patch(
                "openusage_bar.lifecycle_state.LifecycleStatePaths.for_current_user",
                return_value=paths,
            ), patch.object(
                platform_services, "windows_task_xml", return_value="<Task />"
            ), patch.object(platform_services, "_run"):
                platform_services.install_service(interval=300)

            self.assertTrue((local_app_data / "openusage-bar-task.xml").exists())
            self.assertFalse((hostile / "openusage-bar-task.xml").exists())

    def test_native_windows_plugin_install_uses_known_folder_not_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            local_app_data = profile / "AppData" / "Local"
            local_app_data.mkdir(parents=True)
            hostile = root / "PRIVATE_HOSTILE_LOCAL"
            hostile.mkdir()
            paths = LifecycleStatePaths(
                platform="win32",
                home=profile,
                local_app_data=local_app_data,
            )
            collector = root / "package" / "collector.exe"
            collector.parent.mkdir()
            collector.write_bytes(b"collector")
            with patch.object(
                platform_services.sys, "platform", "win32"
            ), patch.dict(
                platform_services.os.environ,
                {"LOCALAPPDATA": str(hostile)},
            ), patch(
                "openusage_bar.lifecycle_state.LifecycleStatePaths.for_current_user",
                return_value=paths,
            ), patch.object(
                platform_services, "_plugin_command", return_value=str(collector)
            ), patch.object(platform_services, "_run"):
                platform_services.install_plugin_service()

            self.assertTrue(
                (local_app_data / "openusage-bar-plugin-task.xml").is_file()
            )
            self.assertFalse(
                (hostile / "openusage-bar-plugin-task.xml").exists()
            )

    def test_native_windows_plugin_uninstall_uses_known_folder_and_manager_first(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            local_app_data = profile / "AppData" / "Local"
            local_app_data.mkdir(parents=True)
            authoritative = local_app_data / "openusage-bar-plugin-task.xml"
            authoritative.write_text("owned", encoding="utf-8")
            hostile = root / "PRIVATE_HOSTILE_LOCAL"
            hostile.mkdir()
            hostile_definition = hostile / authoritative.name
            hostile_definition.write_text("preserve", encoding="utf-8")
            paths = LifecycleStatePaths(
                platform="win32",
                home=profile,
                local_app_data=local_app_data,
            )
            calls = []

            def manager(command: list[str]) -> None:
                calls.append((command, authoritative.exists()))

            with patch.object(
                platform_services.sys, "platform", "win32"
            ), patch.dict(
                platform_services.os.environ,
                {"LOCALAPPDATA": str(hostile)},
            ), patch(
                "openusage_bar.lifecycle_state.LifecycleStatePaths.for_current_user",
                return_value=paths,
            ), patch.object(platform_services, "_run", side_effect=manager):
                platform_services.uninstall_plugin_service()

            self.assertEqual(
                calls,
                [([
                    "schtasks", "/Delete", "/TN",
                    platform_services.PLUGIN_WINDOWS_TASK_NAME, "/F",
                ], True)],
            )
            self.assertFalse(authoritative.exists())
            self.assertEqual(
                hostile_definition.read_text(encoding="utf-8"), "preserve"
            )

    def test_windows_install_rejects_symlinked_xml_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            local_app_data = Path(directory) / "Local"
            local_app_data.mkdir()
            definition = local_app_data / "openusage-bar-task.xml"
            foreign = Path(directory) / "foreign.xml"
            foreign.write_text("preserve", encoding="utf-8")
            definition.symlink_to(foreign)
            with patch.object(platform_services.sys, "platform", "win32"), patch.object(
                platform_services, "_windows_service_definition_path", return_value=definition
            ), patch.object(
                platform_services, "windows_task_xml", return_value="<Task />"
            ), patch.object(platform_services, "_run") as run:
                with self.assertRaises(RuntimeError):
                    platform_services.install_service(interval=300)

            self.assertEqual(foreign.read_text(encoding="utf-8"), "preserve")
            run.assert_not_called()

    def test_windows_service_removal_rejects_parent_swap_without_unlinking_foreign_xml(self):
        for action in ("install_rollback", "uninstall"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                local_app_data = root / "Local"
                local_app_data.mkdir()
                definition = local_app_data / "openusage-bar-task.xml"
                if action == "uninstall":
                    definition.write_text("owned", encoding="utf-8")
                original_local = root / "original-local"
                foreign_local = root / "PRIVATE_FOREIGN"
                foreign_local.mkdir()
                foreign_definition = foreign_local / "openusage-bar-task.xml"
                foreign_definition.write_text("preserve", encoding="utf-8")
                swapped = False

                def swap_after_manager_call(command: list[str]) -> None:
                    nonlocal swapped
                    should_swap = (
                        action == "install_rollback" and command[1] == "/Run"
                    ) or (
                        action == "uninstall" and command[1] == "/Delete"
                    )
                    if should_swap and not swapped:
                        local_app_data.rename(original_local)
                        local_app_data.symlink_to(
                            foreign_local, target_is_directory=True
                        )
                        swapped = True
                        if action == "install_rollback":
                            raise platform_services.ServiceCommandError(returncode=1)

                with patch.object(
                    platform_services.sys, "platform", "win32"
                ), patch.object(
                    platform_services,
                    "_windows_service_definition_path",
                    return_value=definition,
                ), patch.object(
                    platform_services, "windows_task_xml", return_value="<Task />"
                ), patch.object(
                    platform_services, "_run", side_effect=swap_after_manager_call
                ):
                    with self.assertRaises(RuntimeError) as raised:
                        if action == "install_rollback":
                            platform_services.install_service(interval=300)
                        else:
                            platform_services.uninstall_service()

                self.assertTrue(swapped)
                self.assertTrue(
                    foreign_definition.exists(),
                    "Windows lifecycle must not unlink foreign XML after a parent swap",
                )
                self.assertEqual(
                    foreign_definition.read_text(encoding="utf-8"), "preserve"
                )
                self.assertNotIn(str(root), str(raised.exception))

    def test_windows_uninstall_removes_task_and_definition_file(self):
        with tempfile.TemporaryDirectory() as directory:
            local_app_data = Path(directory) / "Local"
            local_app_data.mkdir(parents=True)
            definition = local_app_data / "openusage-bar-task.xml"
            definition.write_text("task", encoding="utf-8")
            calls: list[list[str]] = []
            with patch.object(platform_services.sys, "platform", "win32"), patch.dict(
                "os.environ", {"LOCALAPPDATA": str(local_app_data)}
            ), patch.object(platform_services, "_run", side_effect=calls.append):
                platform_services.uninstall_service()

            self.assertEqual(calls, [[
                "schtasks", "/End", "/TN", "OpenUsageBarCollector",
            ], [
                "schtasks", "/Delete", "/TN", "OpenUsageBarCollector", "/F",
            ]])
            self.assertFalse(definition.exists())

    def test_windows_uninstall_is_idempotent_when_task_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            local_app_data = Path(directory) / "Local"
            local_app_data.mkdir(parents=True)
            definition = local_app_data / "openusage-bar-task.xml"
            definition.write_text("stale", encoding="utf-8")
            missing_task = Path(directory) / "Windows" / "System32" / "Tasks" / "OpenUsageBarCollector"
            missing = platform_services.ServiceCommandError(returncode=1)
            with patch.object(platform_services.sys, "platform", "win32"), patch.dict(
                "os.environ", {"LOCALAPPDATA": str(local_app_data)}
            ), patch.object(
                platform_services,
                "_windows_task_definition_path",
                return_value=missing_task,
            ), patch.object(platform_services, "_run", side_effect=missing):
                platform_services.uninstall_service()

            self.assertFalse(definition.exists())

    def test_windows_rc1_does_not_mean_missing_when_task_file_remains(self):
        with tempfile.TemporaryDirectory() as directory:
            local_app_data = Path(directory) / "Local"
            local_app_data.mkdir(parents=True)
            definition = local_app_data / "openusage-bar-task.xml"
            definition.write_text("task", encoding="utf-8")
            canonical_task = Path(directory) / "Windows" / "System32" / "Tasks" / "OpenUsageBarCollector"
            canonical_task.parent.mkdir(parents=True)
            canonical_task.write_text("registered", encoding="utf-8")
            ambiguous = platform_services.ServiceCommandError(returncode=1)
            with patch.object(platform_services.sys, "platform", "win32"), patch.dict(
                "os.environ", {"LOCALAPPDATA": str(local_app_data)}
            ), patch.object(
                platform_services, "_windows_task_definition_path", return_value=canonical_task
            ), patch.object(platform_services, "_run", side_effect=ambiguous):
                with self.assertRaises(platform_services.ServiceCommandError):
                    platform_services.uninstall_service()

            self.assertTrue(definition.exists())
            self.assertTrue(canonical_task.exists())

    def test_windows_end_rc1_for_stopped_registered_task_still_deletes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            local_app_data = Path(directory) / "Local"
            local_app_data.mkdir(parents=True)
            definition = local_app_data / "openusage-bar-task.xml"
            definition.write_text("task", encoding="utf-8")
            canonical_task = Path(directory) / "Windows" / "System32" / "Tasks" / "OpenUsageBarCollector"
            canonical_task.parent.mkdir(parents=True)
            canonical_task.write_text("registered", encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str]) -> None:
                calls.append(command)
                if command[1] == "/End":
                    raise platform_services.ServiceCommandError(returncode=1)

            with patch.object(platform_services.sys, "platform", "win32"), patch.dict(
                "os.environ", {"LOCALAPPDATA": str(local_app_data)}
            ), patch.object(
                platform_services, "_windows_task_definition_path", return_value=canonical_task
            ), patch.object(platform_services, "_run", side_effect=fake_run):
                platform_services.uninstall_service()

            self.assertEqual(
                [command[1] for command in calls],
                ["/End", "/Delete"],
            )
            self.assertFalse(definition.exists())

    def test_windows_uninstall_fails_closed_for_other_task_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            local_app_data = Path(directory) / "Local"
            local_app_data.mkdir(parents=True)
            definition = local_app_data / "openusage-bar-task.xml"
            definition.write_text("task", encoding="utf-8")
            denied = platform_services.ServiceCommandError(returncode=5)
            with patch.object(platform_services.sys, "platform", "win32"), patch.dict(
                "os.environ", {"LOCALAPPDATA": str(local_app_data)}
            ), patch.object(platform_services, "_run", side_effect=denied):
                with self.assertRaises(platform_services.ServiceCommandError):
                    platform_services.uninstall_service()

            self.assertTrue(definition.exists())
