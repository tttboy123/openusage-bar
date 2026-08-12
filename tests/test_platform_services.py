import contextlib
import errno
import hashlib
import os
import plistlib
import shlex
import socket
import stat
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from openusage_bar import platform_services
from openusage_bar.lifecycle_state import LifecycleStatePaths


class _LinuxServiceReaderHarness:
    """Shared external facts for the Linux service-state public reader."""

    def __init__(
        self,
        test_case: unittest.TestCase,
        root: Path,
        *,
        peer_pid: int = 4321,
        unit_bytes: bytes | None = None,
        rebind_runtime: bool = False,
    ) -> None:
        self.test_case = test_case
        self.root = root
        self.uid = os.getuid()
        self.peer_pid = peer_pid
        self.rebind_runtime = rebind_runtime
        self.home = root / "home"
        self.unit = self.home.joinpath(
            ".config", "systemd", "user", "openusage-bar.service"
        )
        self.collector = self.home.joinpath(
            ".local", "share", "usagehub", "runtime", "openusage-collector"
        )
        self.socket_path = self.home.joinpath(
            ".local", "state", "openusage-bar", "openusage.sock"
        )
        self.unit.parent.mkdir(parents=True)
        self.collector.parent.mkdir(parents=True)
        self.argv = (
            str(self.collector),
            "daemon",
            "--interval",
            "300",
            "--api-transport",
            "unix",
            "--api-socket",
            str(self.socket_path),
        )
        self.argv_nul = ("\0".join(self.argv) + "\0").encode("utf-8")
        self.unit_bytes = unit_bytes or platform_services.systemd_unit(
            interval=300,
            api_socket=str(self.socket_path),
            command=str(self.collector),
        ).encode("utf-8")
        self.unit.write_bytes(self.unit_bytes)
        self.unit.chmod(0o600)
        self.unit_metadata = self.unit.stat()
        self.collector_bytes = b"audited packaged collector"
        self.collector.write_bytes(self.collector_bytes)
        self.collector.chmod(0o700)
        self.collector_metadata = self.collector.stat()
        self.original_collector = self.collector.with_name("openusage-collector.original")
        self.foreign_collector_bytes = b"PRIVATE_FOREIGN_COLLECTOR"
        self.same_size_foreign_collector_bytes = b"X" * len(self.collector_bytes)

        self.fake_cmdline = self._write("cmdline", self.argv_nul)
        self.foreign_collector_cmdline = self._write(
            "foreign-collector-cmdline",
            b"/PRIVATE/foreign-collector\0daemon\0",
        )
        self.fake_status = self._write(
            "status",
            (
                "Name:\topenusage-collector\n"
                f"Uid:\t{self.uid}\t{self.uid}\t{self.uid}\t{self.uid}\n"
            ).encode("ascii"),
        )
        self.fake_process_stat = self._proc_stat(
            "stat", 4312, "openusage-collector", self.peer_pid, 987654
        )
        self.foreign_parent_process_stat = self._proc_stat(
            "foreign-parent-stat", 4312, "openusage-collector", self.peer_pid + 1, 987654
        )
        self.collector_cgroup = self._write(
            "collector-cgroup", self._collector_cgroup().encode("ascii")
        )
        self.foreign_collector_cgroup = self._write(
            "foreign-collector-cgroup",
            b"0::/user.slice/foreign.service\n",
        )
        self.peer_status = self._write(
            "peer-status",
            (
                "Name:\tsystemd\n"
                f"Uid:\t{self.uid}\t{self.uid}\t{self.uid}\t{self.uid}\n"
            ).encode("ascii"),
        )
        self.peer_foreign_status = self._write(
            "peer-foreign-status",
            (
                "Name:\tsystemd\n"
                f"Uid:\t{self.uid + 1}\t{self.uid + 1}\t"
                f"{self.uid + 1}\t{self.uid + 1}\n"
            ).encode("ascii"),
        )
        self.peer_process_stat = self._proc_stat(
            "peer-stat", self.peer_pid, "systemd", 1, 246810
        )
        self.peer_foreign_parent_process_stat = self._proc_stat(
            "peer-foreign-parent-stat", self.peer_pid, "systemd", 2, 246810
        )
        self.peer_cgroup = self._write(
            "peer-cgroup", self._manager_cgroup().encode("ascii")
        )
        self.foreign_peer_cgroup = self._write(
            "foreign-peer-cgroup", b"0::/user.slice/foreign.service\n"
        )
        self.peer_cmdline = self._write(
            "peer-cmdline", b"/usr/lib/systemd/systemd\0--user\0"
        )
        self.foreign_peer_cmdline = self._write(
            "foreign-peer-cmdline", b"/usr/lib/systemd/systemd\0"
        )
        self.fake_run = root / "run"
        self.fake_user = self.fake_run / "user"
        self.fake_runtime = self.fake_user / str(self.uid)
        self.fake_runtime.mkdir(parents=True)
        self.fake_run.chmod(0o755)
        self.fake_user.chmod(0o755)
        self.fake_runtime.chmod(0o700)
        self.run_metadata = self._with_uid(self.fake_run.stat(), 0)
        self.user_metadata = self._with_uid(self.fake_user.stat(), 0)
        self.runtime_metadata = self.fake_runtime.stat()
        self.fake_systemd = self.fake_runtime / "systemd"
        self.fake_systemd.mkdir()
        self.fake_systemd.chmod(0o700)
        self.systemd_metadata = self.fake_systemd.stat()
        self.private_metadata = self._socket_metadata(
            self.systemd_metadata.st_ino + 1000, 0o600
        )
        self.drifted_private_metadata = self._socket_metadata(
            self.systemd_metadata.st_ino + 1001, 0o600
        )
        self.owned_runtime_marker = self._write_at(
            self.fake_runtime / "owned-marker", b"owned-runtime"
        )
        self.original_runtime = self.fake_user / f"{self.uid}.original"
        self.replacement_runtime_marker = self.fake_runtime / "replacement-marker"

        self.fake_system_root = root / "system-root"
        self.fake_system_bin = self.fake_system_root / "usr" / "bin"
        self.fake_system_bin.mkdir(parents=True)
        self.fake_systemctl = self._write_at(
            self.fake_system_bin / "systemctl", b"audited systemctl executable"
        )
        self.fake_system_root.chmod(0o755)
        self.fake_system_bin.parent.chmod(0o755)
        self.fake_system_bin.chmod(0o755)
        self.fake_systemctl.chmod(0o755)
        self.system_root_metadata = self._with_uid(self.fake_system_root.stat(), 0)
        self.system_usr_metadata = self._with_uid(self.fake_system_bin.parent.stat(), 0)
        self.system_bin_metadata = self._with_uid(self.fake_system_bin.stat(), 0)
        self.systemctl_metadata = self._with_uid(self.fake_systemctl.stat(), 0)
        self.fake_systemd_executable = self._write(
            "systemd-executable", b"audited systemd user manager"
        )
        self.fake_systemd_executable.chmod(0o755)
        self.systemd_executable_metadata = self._with_uid(
            self.fake_systemd_executable.stat(), 0
        )

        self.run_fd, self.user_fd, self.runtime_fd, self.systemd_fd = (9200, 9201, 9202, 9203)
        (
            self.system_root_fd,
            self.system_usr_fd,
            self.system_bin_fd,
            self.systemctl_fd,
        ) = (9600, 9601, 9602, 9603)
        self.authority = LifecycleStatePaths(platform="linux", home=self.home)
        self.session_env = {
            "HOME": str(self.home),
            "XDG_RUNTIME_DIR": f"/proc/self/fd/{self.runtime_fd}",
            "DBUS_SESSION_BUS_ADDRESS": (
                f"unix:path=/proc/self/fd/{self.systemd_fd}/private"
            ),
            "LC_ALL": "C",
            "LANG": "C",
            "SYSTEMD_COLORS": "0",
            "PAGER": "cat",
        }
        self.manager_command = [
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
        self.happy_manager_stdout = (
            "MainPID=4312\n"
            "DropInPaths=\n"
            "Id=openusage-bar.service\n"
            f"FragmentPath={self.unit}\n"
            "ActiveState=active\n"
            "LoadState=loaded\n"
            "NeedDaemonReload=no\n"
            "UnitFileState=enabled\n"
            "SubState=running\n"
        ).encode("utf-8")
        self.manager_stdout = self.happy_manager_stdout
        self.events: list[str] = []
        self.systemctl_events: list[str] = []
        self.peer_open_flags: list[int] = []
        self.peer_connect_paths: list[str] = []
        self.queued_private_facts: list[os.stat_result] = []
        self.queued_peer_credentials: list[bytes] = []
        self.reject_systemctl_bin = False
        self.private_missing = False
        self.foreign_peer_process_uid = False
        self.manager_provenance_case = "safe"
        self.peer_cmdline_case = "safe"
        self.collector_ownership_case = "safe"
        self.drift_collector_identity_on_second_manager = False
        self.collector_identity_is_foreign = False
        self.swap_collector_cmdline_on_second_manager = False
        self.collector_cmdline_is_foreign = False
        self.rewrite_collector_on_second_manager = False
        self.collector_was_rewritten = False
        self.swap_collector_on_second_manager = False
        self.collector_executable_swapped = False
        self.foreign_collector_identity: tuple[int, int] | None = None
        self.runtime_rebound = False
        self.real_open = os.open
        self.real_stat = os.stat
        self.real_readlink = os.readlink
        self.real_fstat = os.fstat
        self.real_close = os.close

    def _write(self, name: str, payload: bytes) -> Path:
        return self._write_at(self.root / name, payload)

    @staticmethod
    def _write_at(path: Path, payload: bytes) -> Path:
        path.write_bytes(payload)
        return path

    def _proc_stat(
        self, name: str, pid: int, process_name: str, parent: int, start: int
    ) -> Path:
        fields = [str(pid), f"({process_name})", "S"] + ["0"] * 49
        fields[3] = str(parent)
        fields[21] = str(start)
        return self._write(name, (" ".join(fields) + "\n").encode("ascii"))

    @staticmethod
    def _with_uid(metadata: os.stat_result, uid: int) -> os.stat_result:
        values = list(metadata)
        values[4] = uid
        return os.stat_result(values)

    def _socket_metadata(self, inode: int, mode: int) -> os.stat_result:
        return os.stat_result(
            (
                stat.S_IFSOCK | mode,
                inode,
                self.systemd_metadata.st_dev,
                1,
                self.uid,
                self.systemd_metadata.st_gid,
                0,
                0,
                0,
                0,
            )
        )

    def _collector_cgroup(self) -> str:
        return (
            "0::/user.slice/"
            f"user-{self.uid}.slice/user@{self.uid}.service/"
            "app.slice/openusage-bar.service\n"
        )

    def _manager_cgroup(self) -> str:
        return (
            "0::/user.slice/"
            f"user-{self.uid}.slice/user@{self.uid}.service/init.scope\n"
        )

    def open_peer_socket(self, family: int, socket_type: int):
        self.test_case.assertEqual(
            (family, socket_type), (socket.AF_UNIX, socket.SOCK_STREAM)
        )
        self.events.append("peer_socket")
        harness = self

        class PeerSocket:
            def settimeout(self, timeout):
                harness.test_case.assertEqual(timeout, 1.0)
                harness.events.append("peer_timeout")

            def connect(self, path):
                expected = f"/proc/self/fd/{harness.systemd_fd}/private"
                harness.test_case.assertEqual(path, expected)
                harness.peer_connect_paths.append(path)
                harness.events.append("peer_connect")

            def getsockopt(self, level, option, length):
                harness.test_case.assertEqual((level, option, length), (1, 17, 12))
                harness.events.append("peer_credentials")
                if harness.queued_peer_credentials:
                    return harness.queued_peer_credentials.pop(0)
                return struct.pack("3i", harness.peer_pid, harness.uid, os.getgid())

            def close(self):
                harness.events.append("peer_close")

        return PeerSocket()

    def run_manager(self, command, **kwargs):
        if self.rebind_runtime and not self.runtime_rebound:
            self.fake_runtime.rename(self.original_runtime)
            self.fake_runtime.mkdir(mode=0o700)
            self.replacement_runtime_marker.write_bytes(b"replacement-runtime")
            self.runtime_rebound = True
        self.test_case.assertIn("peer_credentials", self.events)
        self.test_case.assertTrue(
            {
                "peer_status",
                "peer_stat",
                "peer_cgroup",
                "peer_exe",
                "peer_exe_stat",
                "peer_public_exe_stat",
            }.issubset(self.events)
        )
        self.test_case.assertEqual(command, self.manager_command)
        self.test_case.assertEqual(
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
                "env": self.session_env,
                "pass_fds": (self.runtime_fd, self.systemd_fd),
            },
        )
        if "manager" in self.events:
            self.test_case.assertTrue(
                {
                    "proc_exe",
                    "proc_exe_stat",
                    "proc_cmdline",
                    "proc_status",
                    "proc_stat",
                }.issubset(self.events)
            )
            if self.swap_collector_on_second_manager:
                self.collector.rename(self.original_collector)
                self.collector.write_bytes(self.foreign_collector_bytes)
                self.collector.chmod(0o700)
                metadata = self.collector.stat()
                self.foreign_collector_identity = (metadata.st_dev, metadata.st_ino)
                self.collector_executable_swapped = True
            if self.swap_collector_cmdline_on_second_manager:
                self.collector_cmdline_is_foreign = True
            if self.rewrite_collector_on_second_manager:
                with self.collector.open("r+b", buffering=0) as output:
                    output.write(self.same_size_foreign_collector_bytes)
                    os.fsync(output.fileno())
                self.collector_was_rewritten = True
            if self.drift_collector_identity_on_second_manager:
                self.collector_identity_is_foreign = True
        self.events.append("manager")
        return platform_services.subprocess.CompletedProcess(
            command, 0, stdout=self.manager_stdout, stderr=b""
        )

    def open_fact(self, path, flags, mode=0o777, *, dir_fd=None):
        raw = os.fspath(path)
        directory_facts = {
            ("/", None): self.system_root_fd,
            ("usr", self.system_root_fd): self.system_usr_fd,
            ("bin", self.system_usr_fd): self.system_bin_fd,
            ("systemctl", self.system_bin_fd): self.systemctl_fd,
            ("/run", None): self.run_fd,
            ("user", self.run_fd): self.user_fd,
            (str(self.uid), self.user_fd): self.runtime_fd,
            ("systemd", self.runtime_fd): self.systemd_fd,
        }
        descriptor = directory_facts.get((raw, dir_fd))
        if descriptor is not None:
            if raw in {"/", "usr", "bin", "systemctl"}:
                self.systemctl_events.append(raw if raw != "/" else "root")
                if raw == "bin" and self.reject_systemctl_bin:
                    raise OSError(errno.ELOOP, "PRIVATE_SYSTEMCTL_BIN_SYMLINK")
            if raw == "systemd":
                self.peer_open_flags.append(flags)
                self.events.append("systemd_open")
            return descriptor
        process_facts = {
            "/proc/4312/cmdline": (
                "proc_cmdline",
                self.foreign_collector_cmdline
                if self.collector_cmdline_is_foreign
                else self.fake_cmdline,
            ),
            "/proc/4312/status": ("proc_status", self.fake_status),
            "/proc/4312/stat": (
                "proc_stat",
                self.foreign_parent_process_stat
                if self.collector_ownership_case == "wrong_ppid"
                or self.collector_identity_is_foreign
                else self.fake_process_stat,
            ),
            "/proc/4312/cgroup": (
                "proc_cgroup",
                self.foreign_collector_cgroup
                if self.collector_ownership_case == "foreign_cgroup"
                else self.collector_cgroup,
            ),
            f"/proc/{self.peer_pid}/status": (
                "peer_status",
                self.peer_foreign_status
                if self.foreign_peer_process_uid
                else self.peer_status,
            ),
            f"/proc/{self.peer_pid}/stat": (
                "peer_stat",
                self.peer_foreign_parent_process_stat
                if self.manager_provenance_case == "wrong_ppid"
                else self.peer_process_stat,
            ),
            f"/proc/{self.peer_pid}/cgroup": (
                "peer_cgroup",
                self.foreign_peer_cgroup
                if self.manager_provenance_case == "foreign_cgroup"
                else self.peer_cgroup,
            ),
            f"/proc/{self.peer_pid}/cmdline": (
                "peer_cmdline",
                self.foreign_peer_cmdline
                if self.peer_cmdline_case == "missing_user"
                else self.peer_cmdline,
            ),
        }
        process_fact = process_facts.get(raw)
        if process_fact is not None:
            event, source = process_fact
            self.events.append(event)
            return self.real_open(source, flags)
        if dir_fd is None:
            return self.real_open(path, flags, mode)
        return self.real_open(path, flags, mode, dir_fd=dir_fd)

    def stat_fact(self, path, *args, **kwargs):
        raw = os.fspath(path)
        if raw == "bus":
            raise AssertionError("session bus must not be probed")
        if raw == "/proc/4312/exe":
            self.events.append("proc_exe_stat")
            if self.rewrite_collector_on_second_manager:
                return self.real_stat(self.collector)
            return self.collector_metadata
        if raw == f"/proc/{self.peer_pid}/exe":
            self.events.append("peer_exe_stat")
            return self.systemd_executable_metadata
        if raw == "/usr/lib/systemd/systemd":
            self.events.append("peer_public_exe_stat")
            return self.systemd_executable_metadata
        if raw == "systemctl" and kwargs.get("dir_fd") == self.system_bin_fd:
            return self.systemctl_metadata
        if raw == "private" and kwargs.get("dir_fd") == self.systemd_fd:
            self.events.append("private_stat")
            if self.private_missing:
                raise FileNotFoundError("PRIVATE_SYSTEMD_SOCKET_MISSING")
            if self.queued_private_facts:
                return self.queued_private_facts.pop(0)
            return self.private_metadata
        if raw == "systemd" and kwargs.get("dir_fd") == self.runtime_fd:
            return self.systemd_metadata
        return self.real_stat(path, *args, **kwargs)

    def fstat_fact(self, descriptor):
        return {
            self.run_fd: self.run_metadata,
            self.user_fd: self.user_metadata,
            self.runtime_fd: self.runtime_metadata,
            self.systemd_fd: self.systemd_metadata,
            self.system_root_fd: self.system_root_metadata,
            self.system_usr_fd: self.system_usr_metadata,
            self.system_bin_fd: self.system_bin_metadata,
            self.systemctl_fd: self.systemctl_metadata,
        }.get(descriptor) or self.real_fstat(descriptor)

    def close_fact(self, descriptor):
        if descriptor in {self.run_fd, self.user_fd, self.runtime_fd, self.systemd_fd}:
            if descriptor == self.runtime_fd:
                self.events.append("runtime_close")
            if descriptor == self.systemd_fd:
                self.events.append("systemd_close")
            return None
        if descriptor in {
            self.system_root_fd,
            self.system_usr_fd,
            self.system_bin_fd,
            self.systemctl_fd,
        }:
            self.systemctl_events.append(f"close:{descriptor}")
            return None
        return self.real_close(descriptor)

    def readlink_fact(self, path, *args, **kwargs):
        raw = os.fspath(path)
        if raw == "/proc/4312/exe":
            self.events.append("proc_exe")
            return str(self.collector)
        if raw == f"/proc/{self.peer_pid}/exe":
            self.events.append("peer_exe")
            return "/usr/lib/systemd/systemd"
        return self.real_readlink(path, *args, **kwargs)

    @contextlib.contextmanager
    def patched(self, *, manager_side_effect=None):
        with contextlib.ExitStack() as stack:
            enter = stack.enter_context
            enter(patch.object(platform_services.sys, "platform", "linux"))
            enter(
                patch.object(
                    LifecycleStatePaths,
                    "for_current_user",
                    return_value=self.authority,
                )
            )
            self.systemctl_which = enter(
                patch.object(
                    platform_services.shutil,
                    "which",
                    return_value="/usr/bin/systemctl",
                )
            )
            enter(
                patch.dict(
                    os.environ,
                    {
                        "HOME": "PRIVATE_FOREIGN_HOME",
                        "XDG_RUNTIME_DIR": "/PRIVATE/foreign-runtime",
                        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/PRIVATE/foreign-bus",
                    },
                    clear=True,
                )
            )
            self.unbounded = enter(
                patch.object(
                    platform_services.subprocess,
                    "run",
                    side_effect=AssertionError(
                        "unbounded manager capture is forbidden"
                    ),
                )
            )
            self.bounded = enter(
                patch(
                    "openusage_bar.bounded_process.run_bounded",
                    side_effect=manager_side_effect or self.run_manager,
                )
            )
            enter(patch.object(socket, "SO_PEERCRED", 17, create=True))
            self.peer_socket_factory = enter(
                patch.object(socket, "socket", side_effect=self.open_peer_socket)
            )
            for name, handler in (
                ("open", self.open_fact),
                ("stat", self.stat_fact),
                ("fstat", self.fstat_fact),
                ("close", self.close_fact),
                ("readlink", self.readlink_fact),
            ):
                enter(
                    patch.object(
                        platform_services.os, name, side_effect=handler
                    )
                )
            yield self

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
    @unittest.skipIf(os.name == "nt", "requires native Linux authority facts")
    def test_linux_service_absence_state_proves_only_one_stable_negative_manager_fact(
        self,
    ):
        from openusage_bar.platform_services import (
            LinuxCollectorServiceAbsenceState,
            read_current_user_collector_service_absence_state,
        )

        with tempfile.TemporaryDirectory() as directory:
            harness = _LinuxServiceReaderHarness(self, Path(directory))
            harness.unit.unlink()
            absence_stdout = (
                "Job=\n"
                "ControlPID=0\n"
                "MainPID=0\n"
                "DropInPaths=\n"
                "Id=openusage-bar.service\n"
                "FragmentPath=\n"
                "ActiveState=inactive\n"
                "LoadState=not-found\n"
                "NeedDaemonReload=no\n"
                "UnitFileState=\n"
                "SubState=dead\n"
            ).encode("utf-8")
            manager_calls: list[bytes] = []
            negative_command = [
                *harness.manager_command[:-1],
                "--property=ControlPID",
                "--property=Job",
                "--all",
                harness.manager_command[-1],
            ]

            def negative_manager(command, **kwargs):
                self.assertEqual(command, negative_command)
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
                        "env": harness.session_env,
                        "pass_fds": (
                            harness.runtime_fd,
                            harness.systemd_fd,
                        ),
                    },
                )
                manager_calls.append(harness.manager_stdout)
                return platform_services.subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=harness.manager_stdout,
                    stderr=b"",
                )

            harness.private_metadata = harness._socket_metadata(
                harness.systemd_metadata.st_ino + 1000, 0o700
            )
            with harness.patched(manager_side_effect=negative_manager):
                harness.manager_stdout = absence_stdout
                self.assertEqual(
                    read_current_user_collector_service_absence_state(),
                    LinuxCollectorServiceAbsenceState(
                        unit_missing=True,
                        unit_id="openusage-bar.service",
                        load_state="not-found",
                        active_state="inactive",
                        sub_state="dead",
                        unit_file_state=None,
                        main_pid=0,
                        control_pid=0,
                        job=None,
                        fragment_path=None,
                        drop_in_paths=(),
                        needs_reload=False,
                    ),
                )
                self.assertEqual(manager_calls, [absence_stdout, absence_stdout])

                hostile_states = (
                    absence_stdout.replace(
                        b"LoadState=not-found\n",
                        b"LoadState=loaded\n",
                    ).replace(
                        b"FragmentPath=\n",
                        f"FragmentPath={harness.unit}\n".encode(),
                    ),
                    absence_stdout.replace(
                        b"ActiveState=inactive\n",
                        b"ActiveState=activating\n",
                    ),
                )
                for hostile_stdout in hostile_states:
                    with self.subTest(stdout=hostile_stdout):
                        manager_calls.clear()
                        harness.manager_stdout = hostile_stdout
                        with self.assertRaises(
                            platform_services.ServiceCommandError
                        ) as unavailable:
                            read_current_user_collector_service_absence_state()
                        self.assertEqual(
                            str(unavailable.exception),
                            "service activation command failed",
                        )
                        self.assertNotIn("PRIVATE", str(unavailable.exception))

        with tempfile.TemporaryDirectory() as directory:
            harness = _LinuxServiceReaderHarness(self, Path(directory))
            harness.unit.unlink()
            absence_stdout = (
                "Job=\n"
                "ControlPID=0\n"
                "MainPID=0\n"
                "DropInPaths=\n"
                "Id=openusage-bar.service\n"
                "FragmentPath=\n"
                "ActiveState=inactive\n"
                "LoadState=not-found\n"
                "NeedDaemonReload=no\n"
                "UnitFileState=\n"
                "SubState=dead\n"
            ).encode("utf-8")
            negative_command = [
                *harness.manager_command[:-1],
                "--property=ControlPID",
                "--property=Job",
                "--all",
                harness.manager_command[-1],
            ]
            manager_calls = 0
            concurrent_unit_bytes = b"PRIVATE_CONCURRENT_UNIT"

            def create_unit_after_second_manager(command, **kwargs):
                nonlocal manager_calls
                self.assertEqual(command, negative_command)
                self.assertEqual(
                    kwargs["pass_fds"],
                    (harness.runtime_fd, harness.systemd_fd),
                )
                manager_calls += 1
                if manager_calls == 2:
                    harness.unit.write_bytes(concurrent_unit_bytes)
                    harness.unit.chmod(0o600)
                return platform_services.subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=absence_stdout,
                    stderr=b"",
                )

            with harness.patched(
                manager_side_effect=create_unit_after_second_manager
            ):
                with self.assertRaises(
                    platform_services.ServiceCommandError
                ) as concurrent:
                    read_current_user_collector_service_absence_state()

            self.assertEqual(manager_calls, 2)
            self.assertEqual(
                str(concurrent.exception),
                "service activation command failed",
            )
            self.assertNotIn("PRIVATE", str(concurrent.exception))
            self.assertTrue(harness.unit.is_file())
            unit_metadata = harness.unit.lstat()
            self.assertTrue(stat.S_ISREG(unit_metadata.st_mode))
            self.assertEqual(harness.unit.read_bytes(), concurrent_unit_bytes)
            for event in ("peer_close", "systemd_close", "runtime_close"):
                self.assertIn(event, harness.events)

        with tempfile.TemporaryDirectory() as directory:
            harness = _LinuxServiceReaderHarness(self, Path(directory))
            harness.unit.unlink()
            harness.home.chmod(0o777)
            manager_calls: list[tuple[object, ...]] = []

            def forbidden_manager(*args, **kwargs):
                manager_calls.append((args, kwargs))
                return platform_services.subprocess.CompletedProcess(
                    args[0],
                    0,
                    stdout=(
                        b"Job=\nControlPID=0\nMainPID=0\nDropInPaths=\n"
                        b"Id=openusage-bar.service\nFragmentPath=\n"
                        b"ActiveState=inactive\nLoadState=not-found\n"
                        b"NeedDaemonReload=no\nUnitFileState=\nSubState=dead\n"
                    ),
                    stderr=b"",
                )

            class MissingOpenAt2:
                restype = None

                def __call__(self, *args):
                    del args
                    return -1

            fake_libc = type(
                "FakeLibc",
                (),
                {"syscall": MissingOpenAt2()},
            )()

            with harness.patched(manager_side_effect=forbidden_manager), patch.object(
                platform_services.os,
                "uname",
                return_value=type("Uname", (), {"sysname": "Linux"})(),
            ), patch.object(
                platform_services.ctypes,
                "CDLL",
                return_value=fake_libc,
            ), patch.object(
                platform_services.ctypes,
                "set_errno",
            ), patch.object(
                platform_services.ctypes,
                "get_errno",
                return_value=errno.ENOENT,
            ), patch.object(
                platform_services.os,
                "O_PATH",
                0x200000,
                create=True,
            ), patch.object(
                platform_services.os,
                "O_CLOEXEC",
                0x80000,
                create=True,
            ), patch.object(
                platform_services.os,
                "O_NOFOLLOW",
                0x20000,
                create=True,
            ):
                with self.assertRaises(
                    platform_services.ServiceCommandError
                ) as unsafe_home:
                    read_current_user_collector_service_absence_state()

            self.assertEqual(
                str(unsafe_home.exception),
                "service activation command failed",
            )
            self.assertNotIn(str(harness.home), str(unsafe_home.exception))
            self.assertEqual(manager_calls, [])

    def test_linux_service_state_binds_manager_process_and_unit_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = _LinuxServiceReaderHarness(
                self, Path(directory), rebind_runtime=True
            )

            def read_success():
                observed = platform_services.read_current_user_collector_service_state()
                captured = tuple(harness.events)
                harness.events.clear()
                return observed, captured

            def read_failure():
                with self.assertRaises(platform_services.ServiceCommandError) as raised:
                    platform_services.read_current_user_collector_service_state()
                captured = tuple(harness.events)
                harness.events.clear()
                return raised.exception, captured

            def assert_path_free(error):
                self.assertEqual(str(error), "service activation command failed")
                self.assertNotIn("PRIVATE", str(error))

            def assert_closed(captured):
                for event in ("peer_close", "systemd_close", "runtime_close"):
                    self.assertIn(event, captured)

            with harness.patched():
                observed, happy_events = read_success()
                observed_headless, headless_events = read_success()
                happy_collector_metadata = harness.collector_metadata

                ownership_failures = []
                for ownership_case in ("wrong_ppid", "foreign_cgroup"):
                    harness.collector_ownership_case = ownership_case
                    error, captured = read_failure()
                    ownership_failures.append(
                        (ownership_case, error, captured)
                    )
                harness.collector_ownership_case = "safe"

                harness.manager_stdout = harness.manager_stdout.replace(
                    b"DropInPaths=\n",
                    b"DropInPaths=/PRIVATE/drop-in.conf\n",
                )
                hostile_drop_in, hostile_events = read_failure()
                harness.manager_stdout = harness.happy_manager_stdout

                harness.queued_private_facts.extend(
                    [harness.private_metadata, harness.drifted_private_metadata]
                )
                drifted_private, drifted_private_events = read_failure()

                harness.queued_peer_credentials.extend(
                    [
                        struct.pack(
                            "3i", harness.peer_pid, harness.uid, os.getgid()
                        ),
                        struct.pack(
                            "3i", harness.peer_pid + 1, harness.uid, os.getgid()
                        ),
                    ]
                )
                drifted_peer_credentials, drifted_peer_events = read_failure()

                harness.drift_collector_identity_on_second_manager = True
                drifted_collector_identity, drifted_collector_identity_events = read_failure()
                harness.drift_collector_identity_on_second_manager = False
                harness.collector_identity_is_foreign = False

                harness.swap_collector_cmdline_on_second_manager = True
                drifted_collector_cmdline, drifted_collector_cmdline_events = read_failure()
                harness.swap_collector_cmdline_on_second_manager = False
                harness.collector_cmdline_is_foreign = False

                harness.rewrite_collector_on_second_manager = True
                rewritten_collector, rewritten_collector_events = read_failure()
                harness.rewrite_collector_on_second_manager = False
                self.assertTrue(harness.collector_was_rewritten)
                self.assertEqual(
                    (
                        harness.collector.stat().st_dev,
                        harness.collector.stat().st_ino,
                        harness.collector.stat().st_size,
                    ),
                    (
                        harness.collector_metadata.st_dev,
                        harness.collector_metadata.st_ino,
                        harness.collector_metadata.st_size,
                    ),
                )
                self.assertEqual(
                    harness.collector.read_bytes(),
                    harness.same_size_foreign_collector_bytes,
                )
                with harness.collector.open("r+b", buffering=0) as output:
                    output.write(harness.collector_bytes)
                    os.fsync(output.fileno())
                harness.collector_metadata = harness.real_stat(
                    harness.collector
                )

                manager_provenance_failures = []
                for provenance_case in ("wrong_ppid", "foreign_cgroup"):
                    harness.manager_provenance_case = provenance_case
                    manager_calls_before = harness.bounded.call_count
                    error, captured = read_failure()
                    manager_provenance_failures.append(
                        (
                            provenance_case,
                            error,
                            captured,
                            harness.bounded.call_count - manager_calls_before,
                        )
                    )
                harness.manager_provenance_case = "safe"

                harness.swap_collector_on_second_manager = True
                swapped_collector, swapped_collector_events = read_failure()
                harness.swap_collector_on_second_manager = False

                harness.foreign_peer_process_uid = True
                manager_calls_before_foreign_peer = harness.bounded.call_count
                foreign_peer, foreign_peer_events = read_failure()
                harness.foreign_peer_process_uid = False

                harness.peer_cmdline_case = "missing_user"
                manager_calls_before_foreign_peer_cmdline = (
                    harness.bounded.call_count
                )
                foreign_peer_cmdline_error, foreign_peer_cmdline_events = read_failure()
                harness.peer_cmdline_case = "safe"

                harness.systemctl_events.clear()
                harness.reject_systemctl_bin = True
                manager_calls_before_unsafe_systemctl = harness.bounded.call_count
                unsafe_systemctl, _ = read_failure()
                unsafe_systemctl_events = tuple(harness.systemctl_events)
                harness.systemctl_events.clear()
                harness.reject_systemctl_bin = False

                harness.private_missing = True
                manager_calls_before_missing_private = harness.bounded.call_count
                missing_private, missing_private_events = read_failure()

            for error in (
                hostile_drop_in,
                drifted_private,
                drifted_peer_credentials,
                drifted_collector_identity,
                drifted_collector_cmdline,
                rewritten_collector,
                swapped_collector,
                foreign_peer,
                foreign_peer_cmdline_error,
                unsafe_systemctl,
                missing_private,
            ):
                assert_path_free(error)

            self.assertEqual(harness.systemctl_which.call_count, 17)
            self.assertTrue(
                all(
                    call.args == ("systemctl",) and call.kwargs == {}
                    for call in harness.systemctl_which.call_args_list
                )
            )
            harness.unbounded.assert_not_called()
            for manager_calls_before in (
                manager_calls_before_foreign_peer,
                manager_calls_before_foreign_peer_cmdline,
                manager_calls_before_unsafe_systemctl,
                manager_calls_before_missing_private,
            ):
                self.assertEqual(
                    harness.bounded.call_count, manager_calls_before
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
            for close_event in ("systemd_close", "runtime_close"):
                self.assertGreater(
                    happy_events.index(close_event),
                    len(happy_events)
                    - 1
                    - happy_events[::-1].index("manager"),
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
                    assert_closed(post_transaction_events)

            self.assertIn("peer_status", foreign_peer_events)
            assert_closed(foreign_peer_events)
            self.assertNotIn("manager", foreign_peer_events)
            for (
                provenance_case,
                provenance_error,
                provenance_events,
                manager_calls_during_provenance,
            ) in manager_provenance_failures:
                with self.subTest(manager_provenance=provenance_case):
                    assert_path_free(provenance_error)
                    self.assertEqual(manager_calls_during_provenance, 0)
                    self.assertIn("peer_stat", provenance_events)
                    if provenance_case == "foreign_cgroup":
                        self.assertIn("peer_cgroup", provenance_events)
                    self.assertNotIn("manager", provenance_events)
                    assert_closed(provenance_events)

            self.assertIn("peer_cmdline", foreign_peer_cmdline_events)
            self.assertNotIn("manager", foreign_peer_cmdline_events)
            assert_closed(foreign_peer_cmdline_events)
            self.assertEqual(
                unsafe_systemctl_events,
                (
                    "root",
                    "usr",
                    "bin",
                    f"close:{harness.system_usr_fd}",
                    f"close:{harness.system_root_fd}",
                ),
            )
            self.assertIn("private_stat", missing_private_events)
            self.assertNotIn("peer_socket", missing_private_events)
            self.assertNotIn("manager", missing_private_events)
            self.assertIn("systemd_close", missing_private_events)
            self.assertIn("runtime_close", missing_private_events)
            self.assertTrue(harness.peer_open_flags)
            self.assertTrue(
                all(
                    flags & getattr(os, "O_DIRECTORY", 0)
                    and flags & getattr(os, "O_NOFOLLOW", 0)
                    for flags in harness.peer_open_flags
                )
            )
            self.assertEqual(harness.peer_socket_factory.call_count, 16)

            self.assertEqual(observed_headless, observed)
            self.assertEqual(headless_events.count("manager"), 2)
            self.assertIn("peer_credentials", headless_events)
            self.assertEqual(headless_events.count("proc_cgroup"), 2)
            for ownership_case, ownership_error, ownership_events in (
                ownership_failures
            ):
                assert_path_free(ownership_error)
                self.assertIn("manager", ownership_events)
                self.assertIn("proc_stat", ownership_events)
                if ownership_case == "foreign_cgroup":
                    self.assertIn("proc_cgroup", ownership_events)

            self.assertTrue(harness.peer_connect_paths)
            self.assertTrue(
                all(
                    path == f"/proc/self/fd/{harness.systemd_fd}/private"
                    for path in harness.peer_connect_paths
                )
            )
            self.assertTrue(harness.runtime_rebound)
            self.assertEqual(
                (
                    harness.original_runtime
                    / harness.owned_runtime_marker.name
                ).read_bytes(),
                b"owned-runtime",
            )
            self.assertEqual(
                harness.replacement_runtime_marker.read_bytes(),
                b"replacement-runtime",
            )
            self.assertTrue(harness.collector_executable_swapped)
            self.assertEqual(
                (
                    harness.original_collector.stat().st_dev,
                    harness.original_collector.stat().st_ino,
                ),
                (
                    harness.collector_metadata.st_dev,
                    harness.collector_metadata.st_ino,
                ),
            )
            self.assertEqual(
                harness.original_collector.read_bytes(),
                harness.collector_bytes,
            )
            self.assertEqual(
                (
                    harness.collector.stat().st_dev,
                    harness.collector.stat().st_ino,
                ),
                harness.foreign_collector_identity,
            )
            self.assertEqual(
                harness.collector.read_bytes(),
                harness.foreign_collector_bytes,
            )
            self.assertEqual(
                observed,
                platform_services.LinuxCollectorServiceState(
                    unit_file_id=f"{harness.unit_metadata.st_dev}:{harness.unit_metadata.st_ino}",
                    unit_size_bytes=len(harness.unit_bytes),
                    unit_sha256=hashlib.sha256(harness.unit_bytes).hexdigest(),
                    unit_id="openusage-bar.service",
                    load_state="loaded",
                    active_state="active",
                    sub_state="running",
                    unit_file_state="enabled",
                    fragment_path=harness.unit,
                    drop_in_paths=(),
                    needs_reload=False,
                    main_pid=4312,
                    process_uid=harness.uid,
                    process_start_time_ticks=987654,
                    process_executable=harness.collector,
                    process_executable_file_id=(
                        f"{harness.collector_metadata.st_dev}:"
                        f"{harness.collector_metadata.st_ino}"
                    ),
                    process_executable_signature_sha256=hashlib.sha256(
                        struct.pack(
                            ">9Q",
                            happy_collector_metadata.st_dev,
                            happy_collector_metadata.st_ino,
                            happy_collector_metadata.st_mode,
                            happy_collector_metadata.st_uid,
                            happy_collector_metadata.st_gid,
                            happy_collector_metadata.st_nlink,
                            happy_collector_metadata.st_size,
                            happy_collector_metadata.st_mtime_ns,
                            happy_collector_metadata.st_ctime_ns,
                        )
                    ).hexdigest(),
                    process_argv_nul=harness.argv_nul,
                ),
            )

    def test_linux_service_state_caps_manager_output_before_accumulation(self):
        from openusage_bar.bounded_process import BoundedProcessError

        with tempfile.TemporaryDirectory() as directory:
            unit_bytes = b"[Unit]\nDescription=PRIVATE_OUTPUT_LIMIT_MARKER\n"
            harness = _LinuxServiceReaderHarness(
                self,
                Path(directory),
                peer_pid=4322,
                unit_bytes=unit_bytes,
            )

            with harness.patched(
                manager_side_effect=BoundedProcessError("output_overflow")
            ):
                with self.assertRaises(
                    platform_services.ServiceCommandError
                ) as overflow:
                    platform_services.read_current_user_collector_service_state()

            self.assertEqual(
                str(overflow.exception), "service activation command failed"
            )
            self.assertNotIn("PRIVATE", str(overflow.exception))
            harness.unbounded.assert_not_called()
            harness.bounded.assert_called_once_with(
                harness.manager_command,
                timeout=5,
                stdout_limit=64 * 1024,
                stderr_limit=0,
                shell=False,
                stdin=platform_services.subprocess.DEVNULL,
                stdout=platform_services.subprocess.PIPE,
                stderr=platform_services.subprocess.DEVNULL,
                check=False,
                env=harness.session_env,
                pass_fds=(harness.runtime_fd, harness.systemd_fd),
            )
            self.assertEqual(harness.unit.read_bytes(), unit_bytes)

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
