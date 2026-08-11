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
    def test_linux_service_state_binds_manager_process_and_unit_facts(self):
        import errno
        import hashlib
        import os
        import socket
        import stat
        import struct

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            unit = home / ".config" / "systemd" / "user" / "openusage-bar.service"
            collector = (
                home
                / ".local"
                / "share"
                / "usagehub"
                / "runtime"
                / "openusage-collector"
            )
            socket_path = (
                home / ".local" / "state" / "openusage-bar" / "openusage.sock"
            )
            unit.parent.mkdir(parents=True)
            collector.parent.mkdir(parents=True)
            unit_bytes = platform_services.systemd_unit(
                interval=300,
                api_socket=str(socket_path),
                command=str(collector),
            ).encode("utf-8")
            unit.write_bytes(unit_bytes)
            unit.chmod(0o600)
            collector.write_bytes(b"audited packaged collector")
            collector.chmod(0o700)
            collector_metadata = collector.stat()
            original_collector = collector.with_name(
                "openusage-collector.original"
            )
            foreign_collector_bytes = b"PRIVATE_FOREIGN_COLLECTOR"
            same_size_foreign_collector_bytes = b"X" * len(
                b"audited packaged collector"
            )
            fake_cmdline = root / "cmdline"
            argv = (
                str(collector),
                "daemon",
                "--interval",
                "300",
                "--api-transport",
                "unix",
                "--api-socket",
                str(socket_path),
            )
            argv_nul = ("\0".join(argv) + "\0").encode("utf-8")
            fake_cmdline.write_bytes(argv_nul)
            foreign_collector_cmdline = root / "foreign-collector-cmdline"
            foreign_collector_cmdline.write_bytes(
                b"/PRIVATE/foreign-collector\0daemon\0"
            )
            fake_status = root / "status"
            process_uid = os.getuid()
            runtime_fd = 9202
            systemd_fd = 9203
            session_env = {
                "HOME": str(home),
                "XDG_RUNTIME_DIR": f"/proc/self/fd/{runtime_fd}",
                "DBUS_SESSION_BUS_ADDRESS": (
                    f"unix:path=/proc/self/fd/{systemd_fd}/private"
                ),
                "LC_ALL": "C",
                "LANG": "C",
                "SYSTEMD_COLORS": "0",
                "PAGER": "cat",
            }
            fake_status.write_text(
                "Name:\topenusage-collector\n"
                f"Uid:\t{process_uid}\t{process_uid}\t{process_uid}\t{process_uid}\n",
                encoding="ascii",
            )
            peer_pid = 4321
            fake_process_stat = root / "stat"
            foreign_parent_process_stat = root / "foreign-parent-stat"
            process_start_time_ticks = 987654
            process_stat_fields = ["4312", "(openusage-collector)", "S"] + [
                "0"
            ] * 49
            process_stat_fields[3] = str(peer_pid)
            process_stat_fields[21] = str(process_start_time_ticks)
            fake_process_stat.write_text(
                " ".join(process_stat_fields) + "\n",
                encoding="ascii",
            )
            foreign_parent_fields = list(process_stat_fields)
            foreign_parent_fields[3] = str(peer_pid + 1)
            foreign_parent_process_stat.write_text(
                " ".join(foreign_parent_fields) + "\n",
                encoding="ascii",
            )
            collector_cgroup = root / "collector-cgroup"
            foreign_collector_cgroup = root / "foreign-collector-cgroup"
            collector_cgroup.write_text(
                "0::/user.slice/"
                f"user-{process_uid}.slice/user@{process_uid}.service/"
                "app.slice/openusage-bar.service\n",
                encoding="ascii",
            )
            foreign_collector_cgroup.write_text(
                "0::/user.slice/foreign.service\n",
                encoding="ascii",
            )
            peer_start_time_ticks = 246810
            peer_status = root / "peer-status"
            peer_foreign_status = root / "peer-foreign-status"
            peer_status.write_text(
                "Name:\tsystemd\n"
                f"Uid:\t{process_uid}\t{process_uid}\t{process_uid}\t{process_uid}\n",
                encoding="ascii",
            )
            peer_foreign_status.write_text(
                "Name:\tsystemd\n"
                f"Uid:\t{process_uid + 1}\t{process_uid + 1}\t"
                f"{process_uid + 1}\t{process_uid + 1}\n",
                encoding="ascii",
            )
            peer_process_stat = root / "peer-stat"
            peer_foreign_parent_process_stat = root / "peer-foreign-parent-stat"
            peer_stat_fields = [str(peer_pid), "(systemd)", "S"] + ["0"] * 49
            peer_stat_fields[3] = "1"
            peer_stat_fields[21] = str(peer_start_time_ticks)
            peer_process_stat.write_text(
                " ".join(peer_stat_fields) + "\n",
                encoding="ascii",
            )
            peer_foreign_parent_fields = list(peer_stat_fields)
            peer_foreign_parent_fields[3] = "2"
            peer_foreign_parent_process_stat.write_text(
                " ".join(peer_foreign_parent_fields) + "\n",
                encoding="ascii",
            )
            peer_cgroup = root / "peer-cgroup"
            foreign_peer_cgroup = root / "foreign-peer-cgroup"
            peer_cgroup.write_text(
                "0::/user.slice/"
                f"user-{process_uid}.slice/user@{process_uid}.service/"
                "init.scope\n",
                encoding="ascii",
            )
            foreign_peer_cgroup.write_text(
                "0::/user.slice/foreign.service\n",
                encoding="ascii",
            )
            peer_cmdline = root / "peer-cmdline"
            foreign_peer_cmdline = root / "foreign-peer-cmdline"
            peer_cmdline.write_bytes(b"/usr/lib/systemd/systemd\0--user\0")
            foreign_peer_cmdline.write_bytes(b"/usr/lib/systemd/systemd\0")
            fake_run = root / "run"
            fake_user = fake_run / "user"
            fake_runtime = fake_user / str(process_uid)
            fake_runtime.mkdir(parents=True)
            fake_run.chmod(0o755)
            fake_user.chmod(0o755)
            fake_runtime.chmod(0o700)
            run_values = list(fake_run.stat())
            run_values[4] = 0
            run_metadata = os.stat_result(run_values)
            user_values = list(fake_user.stat())
            user_values[4] = 0
            user_metadata = os.stat_result(user_values)
            runtime_metadata = fake_runtime.stat()
            fake_systemd = fake_runtime / "systemd"
            fake_systemd.mkdir()
            fake_systemd.chmod(0o700)
            owned_runtime_marker = fake_runtime / "owned-marker"
            owned_runtime_marker.write_bytes(b"owned-runtime")
            original_runtime = fake_user / f"{process_uid}.original"
            replacement_runtime_marker = fake_runtime / "replacement-marker"
            systemd_metadata = fake_systemd.stat()
            private_metadata = os.stat_result(
                (
                    stat.S_IFSOCK | 0o600,
                    systemd_metadata.st_ino + 1000,
                    systemd_metadata.st_dev,
                    1,
                    process_uid,
                    systemd_metadata.st_gid,
                    0,
                    0,
                    0,
                    0,
                )
            )
            drifted_private_metadata = os.stat_result(
                (
                    stat.S_IFSOCK | 0o600,
                    systemd_metadata.st_ino + 1001,
                    systemd_metadata.st_dev,
                    1,
                    process_uid,
                    systemd_metadata.st_gid,
                    0,
                    0,
                    0,
                    0,
                )
            )
            fake_system_root = root / "system-root"
            fake_system_bin = fake_system_root / "usr" / "bin"
            fake_system_bin.mkdir(parents=True)
            fake_systemctl = fake_system_bin / "systemctl"
            fake_systemctl.write_bytes(b"audited systemctl executable")
            fake_system_root.chmod(0o755)
            fake_system_bin.parent.chmod(0o755)
            fake_system_bin.chmod(0o755)
            fake_systemctl.chmod(0o755)

            def root_owned(metadata):
                values = list(metadata)
                values[4] = 0
                return os.stat_result(values)

            system_root_metadata = root_owned(fake_system_root.stat())
            system_usr_metadata = root_owned(fake_system_bin.parent.stat())
            system_bin_metadata = root_owned(fake_system_bin.stat())
            systemctl_metadata = root_owned(fake_systemctl.stat())
            fake_systemd_executable = root / "systemd-executable"
            fake_systemd_executable.write_bytes(b"audited systemd user manager")
            fake_systemd_executable.chmod(0o755)
            systemd_executable_metadata = root_owned(
                fake_systemd_executable.stat()
            )
            run_fd, user_fd = 9200, 9201
            system_root_fd, system_usr_fd, system_bin_fd, systemctl_fd = (
                9600,
                9601,
                9602,
                9603,
            )
            unit_metadata = unit.stat()
            collector_metadata = collector.stat()
            authority = LifecycleStatePaths(platform="linux", home=home)
            happy_manager_stdout = (
                "MainPID=4312\n"
                "DropInPaths=\n"
                "Id=openusage-bar.service\n"
                f"FragmentPath={unit}\n"
                "ActiveState=active\n"
                "LoadState=loaded\n"
                "NeedDaemonReload=no\n"
                "UnitFileState=enabled\n"
                "SubState=running\n"
            ).encode("utf-8")
            manager_stdout = happy_manager_stdout
            manager_command = [
                "/usr/bin/systemctl",
                "--user",
                "show",
                "openusage-bar.service",
                "--property=Id",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=UnitFileState",
                "--property=FragmentPath",
                "--property=DropInPaths",
                "--property=NeedDaemonReload",
                "--property=MainPID",
                "--no-pager",
            ]
            events: list[str] = []
            systemctl_events: list[str] = []
            reject_systemctl_bin = False
            private_missing = False
            foreign_peer_process_uid = False
            manager_provenance_case = "safe"
            peer_cmdline_case = "safe"
            collector_ownership_case = "safe"
            swap_collector_cmdline_on_second_manager = False
            collector_cmdline_is_foreign = False
            rewrite_collector_on_second_manager = False
            collector_was_rewritten = False
            swap_collector_on_second_manager = False
            collector_executable_swapped = False
            foreign_collector_identity = None
            drift_collector_identity_on_second_manager = False
            collector_identity_is_foreign = False
            peer_open_flags: list[int] = []
            peer_connect_paths: list[str] = []
            queued_private_facts: list[os.stat_result] = []
            queued_peer_credentials: list[bytes] = []
            runtime_rebound = False
            test_case = self

            class PeerSocket:
                def settimeout(self, timeout):
                    test_case.assertEqual(timeout, 1.0)
                    events.append("peer_timeout")

                def connect(self, path):
                    test_case.assertEqual(
                        path,
                        f"/proc/self/fd/{systemd_fd}/private",
                    )
                    peer_connect_paths.append(path)
                    events.append("peer_connect")

                def getsockopt(self, level, option, length):
                    test_case.assertEqual((level, option, length), (1, 17, 12))
                    events.append("peer_credentials")
                    if queued_peer_credentials:
                        return queued_peer_credentials.pop(0)
                    return struct.pack("3i", peer_pid, process_uid, os.getgid())

                def close(self):
                    events.append("peer_close")

            def open_peer_socket(family, socket_type):
                self.assertEqual((family, socket_type), (socket.AF_UNIX, socket.SOCK_STREAM))
                events.append("peer_socket")
                return PeerSocket()

            def run_manager(command, **kwargs):
                nonlocal runtime_rebound
                nonlocal collector_executable_swapped
                nonlocal foreign_collector_identity
                nonlocal collector_cmdline_is_foreign
                nonlocal collector_was_rewritten
                nonlocal collector_identity_is_foreign
                if not runtime_rebound:
                    fake_runtime.rename(original_runtime)
                    fake_runtime.mkdir(mode=0o700)
                    replacement_runtime_marker.write_bytes(b"replacement-runtime")
                    runtime_rebound = True
                self.assertIn("peer_credentials", events)
                self.assertTrue(
                    {
                        "peer_status",
                        "peer_stat",
                        "peer_exe",
                        "peer_exe_stat",
                        "peer_public_exe_stat",
                    }.issubset(events)
                )
                self.assertEqual(command, manager_command)
                self.assertEqual(
                    kwargs,
                    {
                        "shell": False,
                        "stdin": platform_services.subprocess.DEVNULL,
                        "stdout": platform_services.subprocess.PIPE,
                        "stderr": platform_services.subprocess.DEVNULL,
                        "timeout": 5,
                        "stdout_limit": 64 * 1024,
                        "stderr_limit": 0,
                        "check": False,
                        "env": session_env,
                        "pass_fds": (runtime_fd, systemd_fd),
                    },
                )
                if "manager" in events:
                    self.assertTrue(
                        {
                            "proc_exe",
                            "proc_exe_stat",
                            "proc_cmdline",
                            "proc_status",
                            "proc_stat",
                        }.issubset(events)
                    )
                    if swap_collector_on_second_manager:
                        collector.rename(original_collector)
                        collector.write_bytes(foreign_collector_bytes)
                        collector.chmod(0o700)
                        foreign_metadata = collector.stat()
                        foreign_collector_identity = (
                            foreign_metadata.st_dev,
                            foreign_metadata.st_ino,
                        )
                        collector_executable_swapped = True
                    if swap_collector_cmdline_on_second_manager:
                        collector_cmdline_is_foreign = True
                    if rewrite_collector_on_second_manager:
                        with collector.open("r+b", buffering=0) as output:
                            output.write(same_size_foreign_collector_bytes)
                            os.fsync(output.fileno())
                        collector_was_rewritten = True
                    if drift_collector_identity_on_second_manager:
                        collector_identity_is_foreign = True
                events.append("manager")
                return platform_services.subprocess.CompletedProcess(
                    command, 0, stdout=manager_stdout, stderr=b""
                )

            real_open = os.open
            real_stat = os.stat
            real_readlink = os.readlink
            real_fstat = os.fstat
            real_close = os.close

            def open_fact(path, flags, mode=0o777, *, dir_fd=None):
                if os.fspath(path) == "/" and dir_fd is None:
                    systemctl_events.append("root")
                    return system_root_fd
                if os.fspath(path) == "usr" and dir_fd == system_root_fd:
                    systemctl_events.append("usr")
                    return system_usr_fd
                if os.fspath(path) == "bin" and dir_fd == system_usr_fd:
                    systemctl_events.append("bin")
                    if reject_systemctl_bin:
                        raise OSError(errno.ELOOP, "PRIVATE_SYSTEMCTL_BIN_SYMLINK")
                    return system_bin_fd
                if os.fspath(path) == "systemctl" and dir_fd == system_bin_fd:
                    systemctl_events.append("systemctl")
                    return systemctl_fd
                if os.fspath(path) == "/run" and dir_fd is None:
                    return run_fd
                if os.fspath(path) == "user" and dir_fd == run_fd:
                    return user_fd
                if os.fspath(path) == str(process_uid) and dir_fd == user_fd:
                    return runtime_fd
                if os.fspath(path) == "systemd" and dir_fd == runtime_fd:
                    peer_open_flags.append(flags)
                    events.append("systemd_open")
                    return systemd_fd
                process_fact = {
                    "/proc/4312/cmdline": (
                        "proc_cmdline",
                        foreign_collector_cmdline
                        if collector_cmdline_is_foreign
                        else fake_cmdline,
                    ),
                    "/proc/4312/status": ("proc_status", fake_status),
                    "/proc/4312/stat": (
                        "proc_stat",
                        foreign_parent_process_stat
                        if collector_ownership_case == "wrong_ppid"
                        or collector_identity_is_foreign
                        else fake_process_stat,
                    ),
                    "/proc/4312/cgroup": (
                        "proc_cgroup",
                        foreign_collector_cgroup
                        if collector_ownership_case == "foreign_cgroup"
                        else collector_cgroup,
                    ),
                    f"/proc/{peer_pid}/status": (
                        "peer_status",
                        peer_foreign_status
                        if foreign_peer_process_uid
                        else peer_status,
                    ),
                    f"/proc/{peer_pid}/stat": (
                        "peer_stat",
                        peer_foreign_parent_process_stat
                        if manager_provenance_case == "wrong_ppid"
                        else peer_process_stat,
                    ),
                    f"/proc/{peer_pid}/cgroup": (
                        "peer_cgroup",
                        foreign_peer_cgroup
                        if manager_provenance_case == "foreign_cgroup"
                        else peer_cgroup,
                    ),
                    f"/proc/{peer_pid}/cmdline": (
                        "peer_cmdline",
                        foreign_peer_cmdline
                        if peer_cmdline_case == "missing_user"
                        else peer_cmdline,
                    ),
                }.get(os.fspath(path))
                if process_fact is not None:
                    event, source = process_fact
                    events.append(event)
                    return real_open(source, flags)
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            def stat_fact(path, *args, **kwargs):
                if os.fspath(path) == "bus":
                    raise AssertionError("session bus must not be probed")
                if os.fspath(path) == "/proc/4312/exe":
                    events.append("proc_exe_stat")
                    if rewrite_collector_on_second_manager:
                        return real_stat(collector)
                    return collector_metadata
                if os.fspath(path) == f"/proc/{peer_pid}/exe":
                    events.append("peer_exe_stat")
                    return systemd_executable_metadata
                if os.fspath(path) == "/usr/lib/systemd/systemd":
                    events.append("peer_public_exe_stat")
                    return systemd_executable_metadata
                if (
                    os.fspath(path) == "systemctl"
                    and kwargs.get("dir_fd") == system_bin_fd
                    and kwargs.get("follow_symlinks") is False
                ):
                    return systemctl_metadata
                if (
                    os.fspath(path) == "private"
                    and kwargs.get("dir_fd") == systemd_fd
                    and kwargs.get("follow_symlinks") is False
                ):
                    events.append("private_stat")
                    if private_missing:
                        raise FileNotFoundError("PRIVATE_SYSTEMD_SOCKET_MISSING")
                    if queued_private_facts:
                        return queued_private_facts.pop(0)
                    return private_metadata
                if (
                    os.fspath(path) == "systemd"
                    and kwargs.get("dir_fd") == runtime_fd
                    and kwargs.get("follow_symlinks") is False
                ):
                    return systemd_metadata
                return real_stat(path, *args, **kwargs)

            def fstat_fact(descriptor):
                return {
                    run_fd: run_metadata,
                    user_fd: user_metadata,
                    runtime_fd: runtime_metadata,
                    systemd_fd: systemd_metadata,
                    system_root_fd: system_root_metadata,
                    system_usr_fd: system_usr_metadata,
                    system_bin_fd: system_bin_metadata,
                    systemctl_fd: systemctl_metadata,
                }.get(descriptor) or real_fstat(descriptor)

            def close_fact(descriptor):
                if descriptor in {run_fd, user_fd, runtime_fd, systemd_fd}:
                    if descriptor == runtime_fd:
                        events.append("runtime_close")
                    if descriptor == systemd_fd:
                        events.append("systemd_close")
                    return None
                if descriptor in {
                    system_root_fd,
                    system_usr_fd,
                    system_bin_fd,
                    systemctl_fd,
                }:
                    systemctl_events.append(f"close:{descriptor}")
                    return None
                return real_close(descriptor)

            def readlink_fact(path, *args, **kwargs):
                if os.fspath(path) == "/proc/4312/exe":
                    events.append("proc_exe")
                    return str(collector)
                if os.fspath(path) == f"/proc/{peer_pid}/exe":
                    events.append("peer_exe")
                    return "/usr/lib/systemd/systemd"
                return real_readlink(path, *args, **kwargs)

            with patch.object(platform_services.sys, "platform", "linux"), patch.object(
                LifecycleStatePaths,
                "for_current_user",
                return_value=authority,
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ) as systemctl_which, patch.dict(
                os.environ,
                {
                    "HOME": "PRIVATE_FOREIGN_HOME",
                    "XDG_RUNTIME_DIR": "/PRIVATE/foreign-runtime",
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/PRIVATE/foreign-bus",
                },
                clear=True,
            ), patch.object(
                platform_services.subprocess,
                "run",
                side_effect=AssertionError("unbounded manager capture is forbidden"),
            ) as unbounded, patch(
                "openusage_bar.bounded_process.run_bounded",
                side_effect=run_manager,
            ) as bounded, patch.object(
                socket, "SO_PEERCRED", 17, create=True
            ), patch.object(
                socket, "socket", side_effect=open_peer_socket
            ) as peer_socket_factory, patch.object(
                platform_services.os, "open", side_effect=open_fact
            ), patch.object(
                platform_services.os, "stat", side_effect=stat_fact
            ), patch.object(
                platform_services.os, "fstat", side_effect=fstat_fact
            ), patch.object(
                platform_services.os, "close", side_effect=close_fact
            ), patch.object(
                platform_services.os, "readlink", side_effect=readlink_fact
            ):
                observed = platform_services.read_current_user_collector_service_state()
                happy_events = tuple(events)
                events.clear()
                observed_headless = (
                    platform_services.read_current_user_collector_service_state()
                )
                headless_events = tuple(events)
                events.clear()
                ownership_failures = []
                for ownership_case in ("wrong_ppid", "foreign_cgroup"):
                    collector_ownership_case = ownership_case
                    with self.assertRaises(
                        platform_services.ServiceCommandError
                    ) as ownership_failure:
                        platform_services.read_current_user_collector_service_state()
                    ownership_failures.append(
                        (
                            ownership_case,
                            ownership_failure.exception,
                            tuple(events),
                        )
                    )
                    events.clear()
                collector_ownership_case = "safe"
                manager_stdout = manager_stdout.replace(
                    b"DropInPaths=\n",
                    b"DropInPaths=/PRIVATE/drop-in.conf\n",
                )
                with self.assertRaises(
                    platform_services.ServiceCommandError
                ) as hostile_drop_in:
                    platform_services.read_current_user_collector_service_state()
                hostile_events = tuple(events)
                events.clear()
                manager_stdout = happy_manager_stdout
                queued_private_facts.extend(
                    [private_metadata, drifted_private_metadata]
                )
                with self.assertRaises(
                    platform_services.ServiceCommandError
                ) as drifted_private:
                    platform_services.read_current_user_collector_service_state()
                drifted_private_events = tuple(events)
                events.clear()
                queued_peer_credentials.extend(
                    [
                        struct.pack("3i", peer_pid, process_uid, os.getgid()),
                        struct.pack("3i", peer_pid + 1, process_uid, os.getgid()),
                    ]
                )
                with self.assertRaises(
                    platform_services.ServiceCommandError
                ) as drifted_peer_credentials:
                    platform_services.read_current_user_collector_service_state()
                drifted_peer_events = tuple(events)
                events.clear()
                drift_collector_identity_on_second_manager = True
                with self.assertRaises(
                    platform_services.ServiceCommandError
                ) as drifted_collector_identity:
                    platform_services.read_current_user_collector_service_state()
                drifted_collector_identity_events = tuple(events)
                events.clear()
                drift_collector_identity_on_second_manager = False
                collector_identity_is_foreign = False
                swap_collector_cmdline_on_second_manager = True
                with self.assertRaises(
                    platform_services.ServiceCommandError
                ) as drifted_collector_cmdline:
                    platform_services.read_current_user_collector_service_state()
                drifted_collector_cmdline_events = tuple(events)
                events.clear()
                swap_collector_cmdline_on_second_manager = False
                collector_cmdline_is_foreign = False
                rewrite_collector_on_second_manager = True
                with self.assertRaises(
                    platform_services.ServiceCommandError
                ) as rewritten_collector:
                    platform_services.read_current_user_collector_service_state()
                rewritten_collector_events = tuple(events)
                events.clear()
                rewrite_collector_on_second_manager = False
                self.assertTrue(collector_was_rewritten)
                self.assertEqual(
                    (collector.stat().st_dev, collector.stat().st_ino),
                    (collector_metadata.st_dev, collector_metadata.st_ino),
                )
                self.assertEqual(
                    collector.stat().st_size, collector_metadata.st_size
                )
                self.assertEqual(
                    collector.read_bytes(), same_size_foreign_collector_bytes
                )
                with collector.open("r+b", buffering=0) as output:
                    output.write(b"audited packaged collector")
                    os.fsync(output.fileno())
                collector_metadata = real_stat(collector)
                manager_provenance_failures = []
                for provenance_case in ("wrong_ppid", "foreign_cgroup"):
                    manager_provenance_case = provenance_case
                    manager_calls_before_provenance = bounded.call_count
                    with self.assertRaises(
                        platform_services.ServiceCommandError
                    ) as provenance_failure:
                        platform_services.read_current_user_collector_service_state()
                    manager_provenance_failures.append(
                        (
                            provenance_case,
                            provenance_failure.exception,
                            tuple(events),
                            bounded.call_count
                            - manager_calls_before_provenance,
                        )
                    )
                    events.clear()
                manager_provenance_case = "safe"
                swap_collector_on_second_manager = True
                with self.assertRaises(
                    platform_services.ServiceCommandError
                ) as swapped_collector:
                    platform_services.read_current_user_collector_service_state()
                swapped_collector_events = tuple(events)
                events.clear()
                swap_collector_on_second_manager = False
                foreign_peer_process_uid = True
                manager_calls_before_foreign_peer = bounded.call_count
                with self.assertRaises(
                    platform_services.ServiceCommandError
                ) as foreign_peer:
                    platform_services.read_current_user_collector_service_state()
                foreign_peer_events = tuple(events)
                events.clear()
                foreign_peer_process_uid = False
                peer_cmdline_case = "missing_user"
                manager_calls_before_foreign_peer_cmdline = bounded.call_count
                with self.assertRaises(
                    platform_services.ServiceCommandError
                ) as foreign_peer_cmdline_error:
                    platform_services.read_current_user_collector_service_state()
                foreign_peer_cmdline_events = tuple(events)
                events.clear()
                peer_cmdline_case = "safe"
                systemctl_events.clear()
                reject_systemctl_bin = True
                manager_calls_before_unsafe_systemctl = bounded.call_count
                with self.assertRaises(
                    platform_services.ServiceCommandError
                ) as unsafe_systemctl:
                    platform_services.read_current_user_collector_service_state()
                unsafe_systemctl_events = tuple(systemctl_events)
                events.clear()
                systemctl_events.clear()
                reject_systemctl_bin = False
                private_missing = True
                manager_calls_before_missing_private = bounded.call_count
                with self.assertRaises(
                    platform_services.ServiceCommandError
                ) as missing_private:
                    platform_services.read_current_user_collector_service_state()
                missing_private_events = tuple(events)

            self.assertEqual(str(hostile_drop_in.exception), "service activation command failed")
            self.assertNotIn("PRIVATE", str(hostile_drop_in.exception))
            self.assertEqual(
                str(drifted_private.exception), "service activation command failed"
            )
            self.assertNotIn("PRIVATE", str(drifted_private.exception))
            self.assertEqual(
                str(drifted_peer_credentials.exception),
                "service activation command failed",
            )
            self.assertNotIn("PRIVATE", str(drifted_peer_credentials.exception))
            self.assertEqual(
                str(drifted_collector_identity.exception),
                "service activation command failed",
            )
            self.assertNotIn("PRIVATE", str(drifted_collector_identity.exception))
            self.assertEqual(
                str(drifted_collector_cmdline.exception),
                "service activation command failed",
            )
            self.assertNotIn("PRIVATE", str(drifted_collector_cmdline.exception))
            self.assertEqual(
                str(rewritten_collector.exception),
                "service activation command failed",
            )
            self.assertNotIn("PRIVATE", str(rewritten_collector.exception))
            self.assertEqual(
                str(swapped_collector.exception),
                "service activation command failed",
            )
            self.assertNotIn("PRIVATE", str(swapped_collector.exception))
            self.assertEqual(str(foreign_peer.exception), "service activation command failed")
            self.assertNotIn("PRIVATE", str(foreign_peer.exception))
            self.assertEqual(
                str(foreign_peer_cmdline_error.exception),
                "service activation command failed",
            )
            self.assertNotIn("PRIVATE", str(foreign_peer_cmdline_error.exception))
            self.assertEqual(str(unsafe_systemctl.exception), "service activation command failed")
            self.assertNotIn("PRIVATE", str(unsafe_systemctl.exception))
            self.assertEqual(str(missing_private.exception), "service activation command failed")
            self.assertNotIn("PRIVATE", str(missing_private.exception))
            self.assertEqual(systemctl_which.call_count, 17)
            self.assertTrue(
                all(call.args == ("systemctl",) for call in systemctl_which.call_args_list)
            )
            unbounded.assert_not_called()
            self.assertEqual(
                bounded.call_count,
                manager_calls_before_foreign_peer,
            )
            self.assertEqual(
                bounded.call_count,
                manager_calls_before_unsafe_systemctl,
            )
            self.assertEqual(
                bounded.call_count,
                manager_calls_before_foreign_peer_cmdline,
            )
            self.assertEqual(
                bounded.call_count,
                manager_calls_before_missing_private,
            )
            self.assertEqual(happy_events.count("manager"), 2)
            self.assertEqual(happy_events.count("proc_cgroup"), 2)
            self.assertIn("peer_credentials", happy_events)
            for peer_event in (
                "peer_status",
                "peer_stat",
                "peer_cgroup",
                "peer_exe",
                "peer_exe_stat",
                "peer_public_exe_stat",
                "peer_cmdline",
            ):
                self.assertEqual(happy_events.count(peer_event), 2)
            self.assertLess(
                happy_events.index("peer_credentials"),
                happy_events.index("manager"),
            )
            self.assertGreater(
                happy_events.index("systemd_close"),
                len(happy_events) - 1 - happy_events[::-1].index("manager"),
            )
            self.assertGreater(
                happy_events.index("runtime_close"),
                len(happy_events) - 1 - happy_events[::-1].index("manager"),
            )
            self.assertEqual(hostile_events.count("manager"), 1)
            self.assertIn("peer_credentials", hostile_events)
            self.assertIn("systemd_close", hostile_events)
            self.assertIn("runtime_close", hostile_events)
            for case, post_transaction_events in (
                ("private", drifted_private_events),
                ("peer", drifted_peer_events),
                ("collector_identity", drifted_collector_identity_events),
                ("collector_cmdline", drifted_collector_cmdline_events),
                ("collector_content", rewritten_collector_events),
                ("collector_path", swapped_collector_events),
            ):
                with self.subTest(post_transaction_case=case):
                    self.assertEqual(
                        post_transaction_events.count("manager"), 2
                    )
                    self.assertIn("peer_close", post_transaction_events)
                    self.assertIn("systemd_close", post_transaction_events)
                    self.assertIn("runtime_close", post_transaction_events)
            self.assertIn("peer_status", foreign_peer_events)
            self.assertNotIn("manager", foreign_peer_events)
            self.assertIn("peer_close", foreign_peer_events)
            self.assertIn("systemd_close", foreign_peer_events)
            self.assertIn("runtime_close", foreign_peer_events)
            for (
                provenance_case,
                provenance_error,
                provenance_events,
                manager_calls_during_provenance,
            ) in manager_provenance_failures:
                with self.subTest(manager_provenance=provenance_case):
                    self.assertEqual(
                        str(provenance_error),
                        "service activation command failed",
                    )
                    self.assertNotIn("PRIVATE", str(provenance_error))
                    self.assertEqual(manager_calls_during_provenance, 0)
                    self.assertIn("peer_stat", provenance_events)
                    if provenance_case == "foreign_cgroup":
                        self.assertIn("peer_cgroup", provenance_events)
                    self.assertNotIn("manager", provenance_events)
                    self.assertIn("peer_close", provenance_events)
                    self.assertIn("systemd_close", provenance_events)
                    self.assertIn("runtime_close", provenance_events)
            self.assertIn("peer_cmdline", foreign_peer_cmdline_events)
            self.assertNotIn("manager", foreign_peer_cmdline_events)
            self.assertIn("peer_close", foreign_peer_cmdline_events)
            self.assertIn("systemd_close", foreign_peer_cmdline_events)
            self.assertIn("runtime_close", foreign_peer_cmdline_events)
            self.assertEqual(
                unsafe_systemctl_events,
                (
                    "root",
                    "usr",
                    "bin",
                    f"close:{system_usr_fd}",
                    f"close:{system_root_fd}",
                ),
            )
            self.assertIn("private_stat", missing_private_events)
            self.assertNotIn("peer_socket", missing_private_events)
            self.assertNotIn("manager", missing_private_events)
            self.assertIn("systemd_close", missing_private_events)
            self.assertIn("runtime_close", missing_private_events)
            self.assertTrue(peer_open_flags)
            self.assertTrue(
                all(
                    flags & getattr(os, "O_DIRECTORY", 0)
                    and flags & getattr(os, "O_NOFOLLOW", 0)
                    for flags in peer_open_flags
                )
            )
            self.assertEqual(peer_socket_factory.call_count, 16)
            self.assertEqual(observed_headless, observed)
            self.assertEqual(headless_events.count("manager"), 2)
            self.assertIn("peer_credentials", headless_events)
            self.assertEqual(headless_events.count("proc_cgroup"), 2)
            for ownership_case, ownership_error, ownership_events in ownership_failures:
                self.assertEqual(
                    str(ownership_error), "service activation command failed"
                )
                self.assertNotIn("PRIVATE", str(ownership_error))
                self.assertIn("manager", ownership_events)
                self.assertIn("proc_stat", ownership_events)
                if ownership_case == "foreign_cgroup":
                    self.assertIn("proc_cgroup", ownership_events)
            self.assertTrue(peer_connect_paths)
            self.assertTrue(
                all(
                    path == f"/proc/self/fd/{systemd_fd}/private"
                    for path in peer_connect_paths
                )
            )
            self.assertTrue(runtime_rebound)
            self.assertTrue(collector_executable_swapped)
            self.assertEqual(
                (
                    original_collector.stat().st_dev,
                    original_collector.stat().st_ino,
                ),
                (collector_metadata.st_dev, collector_metadata.st_ino),
            )
            self.assertEqual(
                original_collector.read_bytes(), b"audited packaged collector"
            )
            self.assertEqual(
                (collector.stat().st_dev, collector.stat().st_ino),
                foreign_collector_identity,
            )
            self.assertEqual(collector.read_bytes(), foreign_collector_bytes)
            self.assertEqual(
                (original_runtime / owned_runtime_marker.name).read_bytes(),
                b"owned-runtime",
            )
            self.assertEqual(
                replacement_runtime_marker.read_bytes(),
                b"replacement-runtime",
            )
            self.assertEqual(
                observed,
                platform_services.LinuxCollectorServiceState(
                    unit_file_id=f"{unit_metadata.st_dev}:{unit_metadata.st_ino}",
                    unit_size_bytes=len(unit_bytes),
                    unit_sha256=hashlib.sha256(unit_bytes).hexdigest(),
                    unit_id="openusage-bar.service",
                    load_state="loaded",
                    active_state="active",
                    sub_state="running",
                    unit_file_state="enabled",
                    fragment_path=unit,
                    drop_in_paths=(),
                    needs_reload=False,
                    main_pid=4312,
                    process_uid=process_uid,
                    process_start_time_ticks=process_start_time_ticks,
                    process_executable=collector,
                    process_executable_file_id=(
                        f"{collector_metadata.st_dev}:{collector_metadata.st_ino}"
                    ),
                    process_argv_nul=argv_nul,
                ),
            )

    def test_linux_service_state_caps_manager_output_before_accumulation(self):
        import os
        import socket
        import stat
        import struct

        from openusage_bar.bounded_process import BoundedProcessError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            unit = home / ".config" / "systemd" / "user" / "openusage-bar.service"
            unit.parent.mkdir(parents=True)
            unit_bytes = b"[Unit]\nDescription=PRIVATE_OUTPUT_LIMIT_MARKER\n"
            unit.write_bytes(unit_bytes)
            unit.chmod(0o600)
            authority = LifecycleStatePaths(platform="linux", home=home)
            uid = os.getuid()
            peer_pid = 4322
            peer_status = root / "peer-status"
            peer_status.write_text(
                "Name:\tsystemd\n"
                f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n",
                encoding="ascii",
            )
            peer_process_stat = root / "peer-stat"
            peer_stat_fields = [str(peer_pid), "(systemd)", "S"] + ["0"] * 49
            peer_stat_fields[3] = "1"
            peer_stat_fields[21] = "135790"
            peer_process_stat.write_text(
                " ".join(peer_stat_fields) + "\n",
                encoding="ascii",
            )
            peer_cmdline = root / "peer-cmdline"
            peer_cmdline.write_bytes(b"/usr/lib/systemd/systemd\0--user\0")
            peer_cgroup = root / "peer-cgroup"
            peer_cgroup.write_text(
                "0::/user.slice/"
                f"user-{uid}.slice/user@{uid}.service/init.scope\n",
                encoding="ascii",
            )
            collector_pid = 4312
            collector_process_stat = root / "collector-stat"
            collector_stat_fields = [
                str(collector_pid),
                "(openusage-collector)",
                "S",
            ] + ["0"] * 49
            collector_stat_fields[3] = str(peer_pid)
            collector_stat_fields[21] = "97531"
            collector_process_stat.write_text(
                " ".join(collector_stat_fields) + "\n",
                encoding="ascii",
            )
            collector_cgroup = root / "collector-cgroup"
            collector_cgroup.write_text(
                "0::/user.slice/"
                f"user-{uid}.slice/user@{uid}.service/"
                "app.slice/openusage-bar.service\n",
                encoding="ascii",
            )
            fake_run = root / "run"
            fake_user = fake_run / "user"
            fake_runtime = fake_user / str(uid)
            fake_runtime.mkdir(parents=True)
            fake_run.chmod(0o755)
            fake_user.chmod(0o755)
            fake_runtime.chmod(0o700)
            run_values = list(fake_run.stat())
            run_values[4] = 0
            run_metadata = os.stat_result(run_values)
            user_values = list(fake_user.stat())
            user_values[4] = 0
            user_metadata = os.stat_result(user_values)
            runtime_metadata = fake_runtime.stat()
            fake_systemd = fake_runtime / "systemd"
            fake_systemd.mkdir()
            fake_systemd.chmod(0o700)
            systemd_metadata = fake_systemd.stat()
            private_metadata = os.stat_result(
                (
                    stat.S_IFSOCK | 0o600,
                    systemd_metadata.st_ino + 1000,
                    systemd_metadata.st_dev,
                    1,
                    uid,
                    systemd_metadata.st_gid,
                    0,
                    0,
                    0,
                    0,
                )
            )
            fake_system_root = root / "system-root"
            fake_system_bin = fake_system_root / "usr" / "bin"
            fake_system_bin.mkdir(parents=True)
            fake_systemctl = fake_system_bin / "systemctl"
            fake_systemctl.write_bytes(b"audited systemctl executable")
            fake_system_root.chmod(0o755)
            fake_system_bin.parent.chmod(0o755)
            fake_system_bin.chmod(0o755)
            fake_systemctl.chmod(0o755)

            def root_owned(metadata):
                values = list(metadata)
                values[4] = 0
                return os.stat_result(values)

            system_root_metadata = root_owned(fake_system_root.stat())
            system_usr_metadata = root_owned(fake_system_bin.parent.stat())
            system_bin_metadata = root_owned(fake_system_bin.stat())
            systemctl_metadata = root_owned(fake_systemctl.stat())
            fake_systemd_executable = root / "systemd-executable"
            fake_systemd_executable.write_bytes(b"audited systemd user manager")
            fake_systemd_executable.chmod(0o755)
            systemd_executable_metadata = root_owned(
                fake_systemd_executable.stat()
            )
            bus_metadata = os.stat_result(
                (
                    stat.S_IFSOCK | 0o666,
                    runtime_metadata.st_ino + 1000,
                    runtime_metadata.st_dev,
                    1,
                    uid,
                    runtime_metadata.st_gid,
                    0,
                    0,
                    0,
                    0,
                )
            )
            run_fd, user_fd, runtime_fd = 9300, 9301, 9302
            systemd_fd = 9303
            system_root_fd, system_usr_fd, system_bin_fd, systemctl_fd = (
                9700,
                9701,
                9702,
                9703,
            )
            real_open = os.open
            real_fstat = os.fstat
            real_stat = os.stat
            real_readlink = os.readlink
            real_close = os.close

            class PeerSocket:
                def settimeout(self, timeout):
                    test_case.assertEqual(timeout, 1.0)

                def connect(self, path):
                    test_case.assertEqual(
                        path,
                        f"/proc/self/fd/{systemd_fd}/private",
                    )

                def getsockopt(self, level, option, length):
                    test_case.assertEqual((level, option, length), (1, 17, 12))
                    return struct.pack("3i", peer_pid, uid, os.getgid())

                def close(self):
                    return None

            test_case = self

            def open_peer_socket(family, socket_type):
                test_case.assertEqual(
                    (family, socket_type),
                    (socket.AF_UNIX, socket.SOCK_STREAM),
                )
                return PeerSocket()

            def open_session(path, flags, mode=0o777, *, dir_fd=None):
                if os.fspath(path) == "/" and dir_fd is None:
                    return system_root_fd
                if os.fspath(path) == "usr" and dir_fd == system_root_fd:
                    return system_usr_fd
                if os.fspath(path) == "bin" and dir_fd == system_usr_fd:
                    return system_bin_fd
                if os.fspath(path) == "systemctl" and dir_fd == system_bin_fd:
                    return systemctl_fd
                if os.fspath(path) == "/run" and dir_fd is None:
                    return run_fd
                if os.fspath(path) == "user" and dir_fd == run_fd:
                    return user_fd
                if os.fspath(path) == str(uid) and dir_fd == user_fd:
                    return runtime_fd
                if os.fspath(path) == "systemd" and dir_fd == runtime_fd:
                    return systemd_fd
                process_fact = {
                    f"/proc/{peer_pid}/status": peer_status,
                    f"/proc/{peer_pid}/stat": peer_process_stat,
                    f"/proc/{peer_pid}/cmdline": peer_cmdline,
                    f"/proc/{peer_pid}/cgroup": peer_cgroup,
                    f"/proc/{collector_pid}/stat": collector_process_stat,
                    f"/proc/{collector_pid}/cgroup": collector_cgroup,
                }.get(os.fspath(path))
                if process_fact is not None:
                    return real_open(process_fact, flags)
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            def fstat_session(descriptor):
                return {
                    run_fd: run_metadata,
                    user_fd: user_metadata,
                    runtime_fd: runtime_metadata,
                    systemd_fd: systemd_metadata,
                    system_root_fd: system_root_metadata,
                    system_usr_fd: system_usr_metadata,
                    system_bin_fd: system_bin_metadata,
                    systemctl_fd: systemctl_metadata,
                }.get(descriptor) or real_fstat(descriptor)

            def close_session(descriptor):
                if descriptor in {
                    run_fd,
                    user_fd,
                    runtime_fd,
                    systemd_fd,
                    system_root_fd,
                    system_usr_fd,
                    system_bin_fd,
                    systemctl_fd,
                }:
                    return None
                return real_close(descriptor)

            def stat_session(path, *args, **kwargs):
                if (
                    os.fspath(path) == "systemctl"
                    and kwargs.get("dir_fd") == system_bin_fd
                    and kwargs.get("follow_symlinks") is False
                ):
                    return systemctl_metadata
                if (
                    os.fspath(path) == "systemd"
                    and kwargs.get("dir_fd") == runtime_fd
                    and kwargs.get("follow_symlinks") is False
                ):
                    return systemd_metadata
                if (
                    os.fspath(path) == "private"
                    and kwargs.get("dir_fd") == systemd_fd
                    and kwargs.get("follow_symlinks") is False
                ):
                    return private_metadata
                if (
                    os.fspath(path) == "bus"
                    and kwargs.get("dir_fd") == runtime_fd
                    and kwargs.get("follow_symlinks") is False
                ):
                    return bus_metadata
                if os.fspath(path) == f"/proc/{peer_pid}/exe":
                    return systemd_executable_metadata
                if os.fspath(path) == "/usr/lib/systemd/systemd":
                    return systemd_executable_metadata
                return real_stat(path, *args, **kwargs)

            def readlink_session(path, *args, **kwargs):
                if os.fspath(path) == f"/proc/{peer_pid}/exe":
                    return "/usr/lib/systemd/systemd"
                return real_readlink(path, *args, **kwargs)

            session_env = {
                "HOME": str(home),
                "XDG_RUNTIME_DIR": f"/proc/self/fd/{runtime_fd}",
                "DBUS_SESSION_BUS_ADDRESS": (
                    f"unix:path=/proc/self/fd/{systemd_fd}/private"
                ),
                "LC_ALL": "C",
                "LANG": "C",
                "SYSTEMD_COLORS": "0",
                "PAGER": "cat",
            }
            manager_command = [
                "/usr/bin/systemctl",
                "--user",
                "show",
                "openusage-bar.service",
                "--property=Id",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=UnitFileState",
                "--property=FragmentPath",
                "--property=DropInPaths",
                "--property=NeedDaemonReload",
                "--property=MainPID",
                "--no-pager",
            ]

            with patch.object(platform_services.sys, "platform", "linux"), patch.object(
                LifecycleStatePaths,
                "for_current_user",
                return_value=authority,
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.dict(
                os.environ,
                {
                    "HOME": "PRIVATE_FOREIGN_HOME",
                    "XDG_RUNTIME_DIR": "/PRIVATE/foreign-runtime",
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/PRIVATE/foreign-bus",
                },
                clear=True,
            ), patch.object(
                platform_services.subprocess,
                "run",
                side_effect=AssertionError("unbounded manager capture is forbidden"),
            ) as unbounded, patch(
                "openusage_bar.bounded_process.run_bounded",
                side_effect=BoundedProcessError("output_overflow"),
            ) as bounded, patch.object(
                socket, "SO_PEERCRED", 17, create=True
            ), patch.object(
                socket, "socket", side_effect=open_peer_socket
            ), patch.object(
                platform_services.os, "open", side_effect=open_session
            ), patch.object(
                platform_services.os, "fstat", side_effect=fstat_session
            ), patch.object(
                platform_services.os, "stat", side_effect=stat_session
            ), patch.object(
                platform_services.os, "close", side_effect=close_session
            ), patch.object(
                platform_services.os, "readlink", side_effect=readlink_session
            ):
                with self.assertRaises(
                    platform_services.ServiceCommandError
                ) as overflow:
                    platform_services.read_current_user_collector_service_state()

            self.assertEqual(
                str(overflow.exception), "service activation command failed"
            )
            self.assertNotIn("PRIVATE", str(overflow.exception))
            unbounded.assert_not_called()
            bounded.assert_called_once_with(
                manager_command,
                timeout=5,
                stdout_limit=64 * 1024,
                stderr_limit=0,
                shell=False,
                stdin=platform_services.subprocess.DEVNULL,
                stdout=platform_services.subprocess.PIPE,
                stderr=platform_services.subprocess.DEVNULL,
                check=False,
                env=session_env,
                pass_fds=(runtime_fd, systemd_fd),
            )
            self.assertEqual(unit.read_bytes(), unit_bytes)

    def test_linux_service_state_rejects_unsafe_session_runtime_before_manager(self):
        import errno
        import os

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            unit = home / ".config" / "systemd" / "user" / "openusage-bar.service"
            unit.parent.mkdir(parents=True)
            unit_bytes = b"[Unit]\nDescription=session authority fixture\n"
            unit.write_bytes(unit_bytes)
            unit.chmod(0o600)
            fake_run = root / "run"
            fake_user = fake_run / "user"
            fake_user.mkdir(parents=True)
            fake_run.chmod(0o755)
            fake_user.chmod(0o755)
            run_values = list(fake_run.stat())
            run_values[4] = 0
            run_metadata = os.stat_result(run_values)
            user_values = list(fake_user.stat())
            user_values[4] = 0
            user_metadata = os.stat_result(user_values)
            authority = LifecycleStatePaths(platform="linux", home=home)
            uid = os.getuid()
            real_open = os.open
            real_fstat = os.fstat
            real_close = os.close
            run_fd = 9100
            user_fd = 9101
            target_flags: list[int] = []
            closed: list[int] = []

            def open_session(path, flags, mode=0o777, *, dir_fd=None):
                if os.fspath(path) == "/run" and dir_fd is None:
                    return run_fd
                if os.fspath(path) == "user" and dir_fd == run_fd:
                    return user_fd
                if os.fspath(path) == str(uid) and dir_fd == user_fd:
                    target_flags.append(flags)
                    raise OSError(errno.ELOOP, "PRIVATE_RUNTIME_SYMLINK")
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            def fstat_session(descriptor):
                if descriptor == run_fd:
                    return run_metadata
                if descriptor == user_fd:
                    return user_metadata
                return real_fstat(descriptor)

            def close_session(descriptor):
                if descriptor in {run_fd, user_fd}:
                    closed.append(descriptor)
                    return None
                return real_close(descriptor)

            with patch.object(platform_services.sys, "platform", "linux"), patch.object(
                LifecycleStatePaths,
                "for_current_user",
                return_value=authority,
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.dict(
                os.environ, {}, clear=True
            ), patch.object(
                platform_services.os, "open", side_effect=open_session
            ), patch.object(
                platform_services.os, "fstat", side_effect=fstat_session
            ), patch.object(
                platform_services.os, "close", side_effect=close_session
            ), patch.object(
                platform_services.subprocess,
                "run",
                side_effect=AssertionError("manager must not run"),
            ) as unbounded, patch(
                "openusage_bar.bounded_process.run_bounded",
                side_effect=AssertionError("manager must not run"),
            ) as manager:
                with self.assertRaises(
                    platform_services.ServiceCommandError
                ) as unsafe_runtime:
                    platform_services.read_current_user_collector_service_state()

            self.assertEqual(
                str(unsafe_runtime.exception), "service activation command failed"
            )
            self.assertNotIn("PRIVATE", str(unsafe_runtime.exception))
            unbounded.assert_not_called()
            manager.assert_not_called()
            self.assertEqual(len(target_flags), 1)
            self.assertTrue(target_flags[0] & getattr(os, "O_DIRECTORY", 0))
            self.assertTrue(target_flags[0] & getattr(os, "O_NOFOLLOW", 0))
            self.assertEqual(closed, [user_fd, run_fd])
            self.assertEqual(unit.read_bytes(), unit_bytes)

    def test_linux_service_state_rejects_untrusted_runtime_ancestor_before_manager(self):
        import os
        import stat

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            unit = home / ".config" / "systemd" / "user" / "openusage-bar.service"
            unit.parent.mkdir(parents=True)
            unit_bytes = b"[Unit]\nDescription=runtime ancestor authority fixture\n"
            unit.write_bytes(unit_bytes)
            unit.chmod(0o600)
            fake_run = root / "run"
            fake_user = fake_run / "user"
            uid = os.getuid()
            fake_runtime = fake_user / str(uid)
            fake_runtime.mkdir(parents=True)
            fake_run.chmod(0o755)
            fake_user.chmod(0o755)
            fake_runtime.chmod(0o700)
            run_values = list(fake_run.stat())
            run_values[4] = 1
            untrusted_run = os.stat_result(run_values)
            user_values = list(fake_user.stat())
            user_values[4] = 0
            user_metadata = os.stat_result(user_values)
            runtime_metadata = fake_runtime.stat()
            bus_metadata = os.stat_result(
                (
                    stat.S_IFSOCK | 0o666,
                    runtime_metadata.st_ino + 1000,
                    runtime_metadata.st_dev,
                    1,
                    uid,
                    runtime_metadata.st_gid,
                    0,
                    0,
                    0,
                    0,
                )
            )
            run_fd, user_fd, runtime_fd = 9500, 9501, 9502
            authority = LifecycleStatePaths(platform="linux", home=home)
            opened: list[str] = []
            closed: list[int] = []
            real_open = os.open
            real_fstat = os.fstat
            real_stat = os.stat
            real_close = os.close

            def open_session(path, flags, mode=0o777, *, dir_fd=None):
                rendered = os.fspath(path)
                if rendered == "/run" and dir_fd is None:
                    opened.append("run")
                    return run_fd
                if rendered == "user" and dir_fd == run_fd:
                    opened.append("user")
                    return user_fd
                if rendered == str(uid) and dir_fd == user_fd:
                    opened.append("runtime")
                    return runtime_fd
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            def fstat_session(descriptor):
                return {
                    run_fd: untrusted_run,
                    user_fd: user_metadata,
                    runtime_fd: runtime_metadata,
                }.get(descriptor) or real_fstat(descriptor)

            def stat_session(path, *args, **kwargs):
                if (
                    os.fspath(path) == "bus"
                    and kwargs.get("dir_fd") == runtime_fd
                    and kwargs.get("follow_symlinks") is False
                ):
                    return bus_metadata
                return real_stat(path, *args, **kwargs)

            def close_session(descriptor):
                if descriptor in {run_fd, user_fd, runtime_fd}:
                    closed.append(descriptor)
                    return None
                return real_close(descriptor)

            with patch.object(platform_services.sys, "platform", "linux"), patch.object(
                LifecycleStatePaths,
                "for_current_user",
                return_value=authority,
            ), patch.object(
                platform_services.shutil, "which", return_value="/usr/bin/systemctl"
            ), patch.dict(
                os.environ, {}, clear=True
            ), patch.object(
                platform_services.os, "open", side_effect=open_session
            ), patch.object(
                platform_services.os, "fstat", side_effect=fstat_session
            ), patch.object(
                platform_services.os, "stat", side_effect=stat_session
            ), patch.object(
                platform_services.os, "close", side_effect=close_session
            ), patch(
                "openusage_bar.bounded_process.run_bounded",
                side_effect=AssertionError("manager must not run"),
            ) as manager:
                with self.assertRaises(
                    platform_services.ServiceCommandError
                ) as unsafe_ancestor:
                    platform_services.read_current_user_collector_service_state()

            self.assertEqual(
                str(unsafe_ancestor.exception), "service activation command failed"
            )
            self.assertNotIn("PRIVATE", str(unsafe_ancestor.exception))
            manager.assert_not_called()
            self.assertEqual(opened, ["run"])
            self.assertEqual(closed, [run_fd])
            self.assertEqual(unit.read_bytes(), unit_bytes)

    def test_linux_service_state_rejects_unsafe_session_bus_before_manager(self):
        import os
        import stat

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            unit = home / ".config" / "systemd" / "user" / "openusage-bar.service"
            unit.parent.mkdir(parents=True)
            unit_bytes = b"[Unit]\nDescription=session bus authority fixture\n"
            unit.write_bytes(unit_bytes)
            unit.chmod(0o600)
            fake_run = root / "run"
            fake_user = fake_run / "user"
            uid = os.getuid()
            fake_runtime = fake_user / str(uid)
            fake_runtime.mkdir(parents=True)
            fake_run.chmod(0o755)
            fake_user.chmod(0o755)
            fake_runtime.chmod(0o700)
            run_values = list(fake_run.stat())
            run_values[4] = 0
            run_metadata = os.stat_result(run_values)
            user_values = list(fake_user.stat())
            user_values[4] = 0
            user_metadata = os.stat_result(user_values)
            runtime_metadata = fake_runtime.stat()
            run_fd, user_fd, runtime_fd = 9400, 9401, 9402
            authority = LifecycleStatePaths(platform="linux", home=home)
            real_open = os.open
            real_fstat = os.fstat
            real_stat = os.stat
            real_close = os.close

            def bus_fact(mode, *, fact_uid=uid, nlink=1):
                return os.stat_result(
                    (
                        mode,
                        runtime_metadata.st_ino + 1000,
                        runtime_metadata.st_dev,
                        nlink,
                        fact_uid,
                        runtime_metadata.st_gid,
                        0,
                        0,
                        0,
                        0,
                    )
                )

            cases = {
                "missing": FileNotFoundError("PRIVATE_MISSING_BUS"),
                "symlink": bus_fact(stat.S_IFLNK | 0o777),
                "non_socket": bus_fact(stat.S_IFREG | 0o600),
                "wrong_uid": bus_fact(stat.S_IFSOCK | 0o666, fact_uid=uid + 1),
                "multiple_links": bus_fact(stat.S_IFSOCK | 0o666, nlink=2),
                "unsafe_mode": bus_fact(stat.S_IFSOCK | 0o766),
            }

            for label, selected_fact in cases.items():
                with self.subTest(case=label):
                    closed: list[int] = []

                    def open_session(path, flags, mode=0o777, *, dir_fd=None):
                        if os.fspath(path) == "/run" and dir_fd is None:
                            return run_fd
                        if os.fspath(path) == "user" and dir_fd == run_fd:
                            return user_fd
                        if os.fspath(path) == str(uid) and dir_fd == user_fd:
                            return runtime_fd
                        if dir_fd is None:
                            return real_open(path, flags, mode)
                        return real_open(path, flags, mode, dir_fd=dir_fd)

                    def fstat_session(descriptor):
                        return {
                            run_fd: run_metadata,
                            user_fd: user_metadata,
                            runtime_fd: runtime_metadata,
                        }.get(descriptor) or real_fstat(descriptor)

                    def stat_session(path, *args, **kwargs):
                        if (
                            os.fspath(path) == "bus"
                            and kwargs.get("dir_fd") == runtime_fd
                            and kwargs.get("follow_symlinks") is False
                        ):
                            if isinstance(selected_fact, BaseException):
                                raise selected_fact
                            return selected_fact
                        return real_stat(path, *args, **kwargs)

                    def close_session(descriptor):
                        if descriptor in {run_fd, user_fd, runtime_fd}:
                            closed.append(descriptor)
                            return None
                        return real_close(descriptor)

                    with patch.object(
                        platform_services.sys, "platform", "linux"
                    ), patch.object(
                        LifecycleStatePaths,
                        "for_current_user",
                        return_value=authority,
                    ), patch.object(
                        platform_services.shutil,
                        "which",
                        return_value="/usr/bin/systemctl",
                    ), patch.dict(
                        os.environ, {}, clear=True
                    ), patch.object(
                        platform_services.os, "open", side_effect=open_session
                    ), patch.object(
                        platform_services.os, "fstat", side_effect=fstat_session
                    ), patch.object(
                        platform_services.os, "stat", side_effect=stat_session
                    ), patch.object(
                        platform_services.os, "close", side_effect=close_session
                    ), patch(
                        "openusage_bar.bounded_process.run_bounded",
                        side_effect=AssertionError("manager must not run"),
                    ) as manager:
                        with self.assertRaises(
                            platform_services.ServiceCommandError
                        ) as unsafe_bus:
                            platform_services.read_current_user_collector_service_state()

                    self.assertEqual(
                        str(unsafe_bus.exception),
                        "service activation command failed",
                    )
                    self.assertNotIn("PRIVATE", str(unsafe_bus.exception))
                    manager.assert_not_called()
                    self.assertEqual(closed, [user_fd, run_fd, runtime_fd])
                    self.assertEqual(unit.read_bytes(), unit_bytes)

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
