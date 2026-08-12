from __future__ import annotations

import errno
import io
import json
import os
import runpy
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, fields, replace
from types import SimpleNamespace
from unittest.mock import patch


@unittest.skipUnless(
    hasattr(os, "getuid")
    and hasattr(os, "getgid")
    and hasattr(socket, "AF_UNIX"),
    "POSIX Unix-socket canary contracts",
)
class OnefileLocalAPICanaryTests(unittest.TestCase):
    def test_private_boundary_reader_rejects_an_untrusted_or_expired_snapshot(self):
        from scripts.canary_onefile_local_api import (
            OnefileLocalAPICanaryError,
            read_onefile_shared_client_boundary_snapshot,
        )

        payload = json.dumps(
            {
                "apiVersion": "local-api-internal-diagnostics/v1",
                "object": "sharedClientBoundaryAttempts",
                "processEpochSha256": "a" * 64,
                "boundedHttpOpenAttempts": 0,
                "headlessKeychainGetAttempts": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            b"Cache-Control: no-store\r\n"
            b"X-Content-Type-Options: nosniff\r\n"
            b"Connection: close\r\n"
            + f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
            + payload
        )

        for label, peer, remaining_values, wire_response in (
            (
                "zero_pid",
                struct.pack("=3i", 0, os.getuid(), os.getgid()),
                [0.5] * 10,
                response,
            ),
            (
                "expired_after_eof",
                struct.pack("=3i", 4312, os.getuid(), os.getgid()),
                [0.5] * 5,
                response,
            ),
            (
                "expired_after_close",
                struct.pack("=3i", 4312, os.getuid(), os.getgid()),
                [0.5] * 8,
                response,
            ),
            (
                "invalid_header_name",
                struct.pack("=3i", 4312, os.getuid(), os.getgid()),
                [0.5] * 10,
                response.replace(
                    b"Content-Type:",
                    b"Bad Name: x\r\nContent-Type:",
                    1,
                ),
            ),
            (
                "invalid_header_value",
                struct.pack("=3i", 4312, os.getuid(), os.getgid()),
                [0.5] * 10,
                response.replace(
                    b"Content-Type: application/json",
                    b"Content-Type:\x0bapplication/json",
                    1,
                ),
            ),
        ):
            with self.subTest(label=label):
                chunks = iter((wire_response[:31], wire_response[31:], b""))
                timeouts = iter(remaining_values)
                events: list[str] = []

                class Client:
                    def settimeout(self, _timeout: float) -> None:
                        pass

                    def connect(self, _path: str) -> None:
                        pass

                    def getsockopt(
                        self, _level: int, _option: int, _size: int
                    ) -> bytes:
                        return peer

                    def sendall(self, _request: bytes) -> None:
                        pass

                    def recv(self, _size: int) -> bytes:
                        return next(chunks)

                    def close(self) -> None:
                        events.append("close")

                with patch(
                    "scripts.canary_onefile_local_api.socket.socket",
                    return_value=Client(),
                ):
                    with self.assertRaisesRegex(
                        OnefileLocalAPICanaryError,
                        "^onefile Local API canary failed$",
                    ):
                        read_onefile_shared_client_boundary_snapshot(
                            "/PRIVATE/openusage.sock",
                            remaining_timeout=lambda: next(timeouts),
                        )

                self.assertEqual(events, ["close"])

    def test_private_boundary_reader_rejects_unclosed_timeout_values(self):
        from scripts.canary_onefile_local_api import (
            OnefileLocalAPICanaryError,
            read_onefile_shared_client_boundary_snapshot,
        )

        for value in (True, float("nan"), float("inf"), 0.0, -0.1, 1.01):
            with self.subTest(value=value):
                events: list[str] = []

                class Client:
                    def close(self) -> None:
                        events.append("close")

                with patch(
                    "scripts.canary_onefile_local_api.socket.socket",
                    return_value=Client(),
                ):
                    with self.assertRaisesRegex(
                        OnefileLocalAPICanaryError,
                        "^onefile Local API canary failed$",
                    ):
                        read_onefile_shared_client_boundary_snapshot(
                            "/PRIVATE/openusage.sock",
                            remaining_timeout=lambda: value,
                        )

                self.assertEqual(events, ["close"])

    def test_shared_boundary_window_rejects_peer_epoch_and_counter_drift(self):
        from openusage_bar.shared_client_boundary import (
            SharedClientBoundaryAttemptCounters,
        )
        from scripts.canary_onefile_local_api import (
            OnefileLocalAPICanaryError,
            evaluate_onefile_shared_client_boundary_window,
        )

        peer = struct.pack("=3i", 4312, os.getuid(), os.getgid())
        zero = SharedClientBoundaryAttemptCounters("a" * 64, 0, 0)
        invalid_epoch = SharedClientBoundaryAttemptCounters("c" * 64, 0, 0)
        object.__setattr__(invalid_epoch, "process_epoch_sha256", "PRIVATE")
        cases = (
            ("wrong_type", object(), zero, peer),
            (
                "epoch_drift",
                zero,
                SharedClientBoundaryAttemptCounters("b" * 64, 0, 0),
                peer,
            ),
            (
                "http_open",
                zero,
                SharedClientBoundaryAttemptCounters("a" * 64, 1, 0),
                peer,
            ),
            (
                "keychain_get",
                zero,
                SharedClientBoundaryAttemptCounters("a" * 64, 0, 1),
                peer,
            ),
            ("peer_drift", zero, zero, struct.pack("=3i", 4313, os.getuid(), os.getgid())),
            ("invalid_peer", zero, zero, b""),
            ("mutated_exact_fact", invalid_epoch, invalid_epoch, peer),
        )
        for label, before, after, peer_after in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    OnefileLocalAPICanaryError,
                    "^onefile Local API canary failed$",
                ):
                    evaluate_onefile_shared_client_boundary_window(
                        health_peer=peer,
                        peer_before=peer,
                        counters_before=before,
                        peer_after=peer_after,
                        counters_after=after,
                    )

    def test_private_boundary_reader_binds_one_unix_peer_and_strict_snapshot(self):
        from openusage_bar.shared_client_boundary import (
            SharedClientBoundaryAttemptCounters,
        )
        from scripts.canary_onefile_local_api import (
            read_onefile_shared_client_boundary_snapshot,
        )

        peer = struct.pack("=3i", 4312, os.getuid(), os.getgid())
        payload = json.dumps(
            {
                "apiVersion": "local-api-internal-diagnostics/v1",
                "object": "sharedClientBoundaryAttempts",
                "processEpochSha256": "a" * 64,
                "boundedHttpOpenAttempts": 0,
                "headlessKeychainGetAttempts": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            b"Cache-Control: no-store\r\n"
            b"X-Content-Type-Options: nosniff\r\n"
            b"Connection: close\r\n"
            + f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
            + payload
        )
        chunks = iter((response[:31], response[31:], b""))
        events: list[str] = []

        class Client:
            def settimeout(self, timeout: float) -> None:
                self_outer.assertEqual(timeout, 0.5)

            def connect(self, path: str) -> None:
                self_outer.assertEqual(path, "/PRIVATE/openusage.sock")
                events.append("connect")

            def getsockopt(self, level: int, option: int, size: int) -> bytes:
                self_outer.assertEqual((level, option, size), (1, 17, 12))
                events.append("peer")
                return peer

            def sendall(self, request: bytes) -> None:
                self_outer.assertEqual(
                    request,
                    b"GET /_internal/v1/shared-client-boundary-attempts HTTP/1.1\r\n"
                    b"Host: localhost\r\nAccept: application/json\r\n"
                    b"Connection: close\r\n\r\n",
                )
                events.append("send")

            def recv(self, size: int) -> bytes:
                self_outer.assertEqual(size, 65_536)
                value = next(chunks)
                events.append("recv")
                return value

            def close(self) -> None:
                events.append("close")

        self_outer = self
        with patch(
            "scripts.canary_onefile_local_api.socket.socket",
            return_value=Client(),
        ):
            observed_peer, counters = read_onefile_shared_client_boundary_snapshot(
                "/PRIVATE/openusage.sock",
                remaining_timeout=lambda: 0.5,
            )

        self.assertEqual(observed_peer, peer)
        self.assertEqual(
            counters,
            SharedClientBoundaryAttemptCounters("a" * 64, 0, 0),
        )
        self.assertEqual(
            events,
            ["connect", "peer", "send", "recv", "recv", "recv", "peer", "close"],
        )

    def test_stable_direct_child_produces_only_closed_boolean_summary(self):
        from scripts.canary_onefile_local_api import (
            OnefileLocalAPISummary,
            OnefileProcessFacts,
            evaluate_onefile_local_api_peer,
        )

        parent_pid = 4300
        child_pid = 4312
        current_uid = os.getuid()
        current_gid = os.getgid()
        service_cgroup = (
            "0::/user.slice/"
            f"user-{current_uid}.slice/user@{current_uid}.service/"
            "app.slice/openusage-bar.service\n"
        )
        private_argv = (
            b"/PRIVATE/openusage-collector\0daemon\0--interval\0"
            b"300\0"
        )

        def process_facts(*, pid: int, ppid: int, start_time: int):
            return OnefileProcessFacts(
                pid=pid,
                uid=current_uid,
                gid=current_gid,
                ppid=ppid,
                start_time_ticks=start_time,
                cgroup=service_cgroup,
                executable_dev=71,
                executable_ino=81,
                executable_size_bytes=91,
                executable_mode=0o100700,
                executable_mtime_ns=101,
                executable_ctime_ns=102,
                executable_nlink=1,
                argv_nul=private_argv,
            )

        parent = process_facts(pid=parent_pid, ppid=4321, start_time=1000)
        child = process_facts(
            pid=child_pid,
            ppid=parent_pid,
            start_time=2000,
        )
        facts = {
            parent_pid: iter((parent, parent)),
            child_pid: iter((child, child)),
        }
        events: list[int] = []

        def read_process_facts(pid: int):
            events.append(pid)
            return next(facts[pid])

        summary = evaluate_onefile_local_api_peer(
            parent_pid=parent_pid,
            peer_credentials=struct.pack(
                "=3i",
                child_pid,
                current_uid,
                current_gid,
            ),
            read_process_facts=read_process_facts,
        )

        self.assertEqual(
            tuple(field.name for field in fields(OnefileProcessFacts)),
            (
                "pid",
                "uid",
                "gid",
                "ppid",
                "start_time_ticks",
                "cgroup",
                "executable_dev",
                "executable_ino",
                "executable_size_bytes",
                "executable_mode",
                "executable_mtime_ns",
                "executable_ctime_ns",
                "executable_nlink",
                "argv_nul",
            ),
        )
        self.assertEqual(
            tuple(field.name for field in fields(OnefileLocalAPISummary)),
            ("stable_direct_child",),
        )
        self.assertEqual(summary, OnefileLocalAPISummary(True))
        self.assertEqual(events, [parent_pid, child_pid, child_pid, parent_pid])
        with self.assertRaises(FrozenInstanceError):
            summary.stable_direct_child = False
        rendered = repr(summary)
        self.assertNotIn("PRIVATE", rendered)
        self.assertNotIn(service_cgroup, rendered)
        self.assertNotIn(str(parent_pid), rendered)
        self.assertNotIn(str(child_pid), rendered)

    def test_unstable_or_foreign_child_facts_fail_closed_without_raw_output(self):
        from scripts.canary_onefile_local_api import (
            OnefileLocalAPICanaryError,
            OnefileProcessFacts,
            evaluate_onefile_local_api_peer,
        )

        parent_pid = 4300
        child_pid = 4312
        current_uid = os.getuid()
        current_gid = os.getgid()
        service_cgroup = (
            "0::/user.slice/"
            f"user-{current_uid}.slice/user@{current_uid}.service/"
            "app.slice/openusage-bar.service\n"
        )
        argv = b"/PRIVATE/openusage-collector\0daemon\0"

        def make_facts(*, pid, ppid, start_time):
            return OnefileProcessFacts(
                pid=pid,
                uid=current_uid,
                gid=current_gid,
                ppid=ppid,
                start_time_ticks=start_time,
                cgroup=service_cgroup,
                executable_dev=71,
                executable_ino=81,
                executable_size_bytes=91,
                executable_mode=0o100700,
                executable_mtime_ns=101,
                executable_ctime_ns=102,
                executable_nlink=1,
                argv_nul=argv,
            )

        parent = make_facts(pid=parent_pid, ppid=4321, start_time=1000)
        child = make_facts(pid=child_pid, ppid=parent_pid, start_time=2000)
        full_order = [parent_pid, child_pid, child_pid, parent_pid]
        peer = struct.pack("=3i", child_pid, current_uid, current_gid)

        def invalid_nlink(facts):
            changed = replace(facts)
            object.__setattr__(changed, "executable_nlink", 2)
            return changed

        hostile_comparisons = [0]

        class HostileFacts(OnefileProcessFacts):
            def __eq__(self, _other):
                hostile_comparisons[0] += 1
                raise RuntimeError("PRIVATE_HOSTILE_COMPARISON")

        hostile_parent = HostileFacts(
            **{
                field.name: getattr(parent, field.name)
                for field in fields(OnefileProcessFacts)
            }
        )
        cases = [
            ("wrong_ppid", peer, (parent, parent), (replace(child, ppid=4322),) * 2),
            ("grandchild", peer, (parent, parent), (replace(child, ppid=4301),) * 2),
            ("child_not_newer", peer, (parent, parent), (replace(child, start_time_ticks=1000),) * 2),
            ("peer_uid", struct.pack("=3i", child_pid, current_uid + 1, current_gid), (parent, parent), (child, child)),
            ("peer_gid", struct.pack("=3i", child_pid, current_uid, current_gid + 1), (parent, parent), (child, child)),
            ("parent_drift", peer, (parent, replace(parent, start_time_ticks=1001)), (child, child)),
            ("child_drift", peer, (parent, parent), (child, replace(child, start_time_ticks=2001))),
            ("foreign_cgroup", peer, (parent, parent), (replace(child, cgroup="0::/PRIVATE/foreign.service\n"),) * 2),
            ("argv", peer, (parent, parent), (replace(child, argv_nul=b"PRIVATE-foreign\0"),) * 2),
            ("executable_dev", peer, (parent, parent), (replace(child, executable_dev=72),) * 2),
            ("executable_ino", peer, (parent, parent), (replace(child, executable_ino=82),) * 2),
            ("executable_size", peer, (parent, parent), (replace(child, executable_size_bytes=92),) * 2),
            ("executable_mode", peer, (parent, parent), (replace(child, executable_mode=0o100500),) * 2),
            ("executable_mtime", peer, (parent, parent), (replace(child, executable_mtime_ns=103),) * 2),
            ("executable_ctime", peer, (parent, parent), (replace(child, executable_ctime_ns=104),) * 2),
            ("executable_nlink", peer, (parent, parent), (invalid_nlink(child),) * 2),
            ("hostile_subclass", peer, (hostile_parent, hostile_parent), (child, child)),
        ]
        for case_name, peer_value, parents, children in cases:
            with self.subTest(case=case_name):
                events = []
                facts = {
                    parent_pid: iter(parents),
                    child_pid: iter(children),
                }

                def read_process_facts(pid):
                    events.append(pid)
                    return next(facts[pid])

                with self.assertRaisesRegex(
                    OnefileLocalAPICanaryError,
                    "onefile Local API canary failed",
                ) as rejected:
                    evaluate_onefile_local_api_peer(
                        parent_pid=parent_pid,
                        peer_credentials=peer_value,
                        read_process_facts=read_process_facts,
                    )
                self.assertEqual(
                    str(rejected.exception),
                    "onefile Local API canary failed",
                )
                self.assertNotIn("PRIVATE", str(rejected.exception))
                self.assertEqual(events, full_order)

        for case_name, parent_value, peer_value, callback in (
            ("same_pid", parent_pid, struct.pack("=3i", parent_pid, current_uid, current_gid), lambda _pid: parent),
            ("parent_bool", True, peer, lambda _pid: parent),
            ("peer_bytearray", parent_pid, bytearray(peer), lambda _pid: parent),
            ("callback_not_callable", parent_pid, peer, object()),
        ):
            with self.subTest(case=case_name):
                calls = []

                def tracked(pid):
                    calls.append(pid)
                    return callback(pid)

                selected_callback = callback if case_name == "callback_not_callable" else tracked
                with self.assertRaisesRegex(
                    OnefileLocalAPICanaryError,
                    "onefile Local API canary failed",
                ) as rejected:
                    evaluate_onefile_local_api_peer(
                        parent_pid=parent_value,
                        peer_credentials=peer_value,
                        read_process_facts=selected_callback,
                    )
                self.assertEqual(str(rejected.exception), "onefile Local API canary failed")
                self.assertNotIn("PRIVATE", str(rejected.exception))
                self.assertEqual(calls, [])

        callback_calls = []

        def failing_callback(pid):
            callback_calls.append(pid)
            raise RuntimeError("PRIVATE_CALLBACK_FAILURE")

        with self.assertRaisesRegex(
            OnefileLocalAPICanaryError,
            "onefile Local API canary failed",
        ) as rejected:
            evaluate_onefile_local_api_peer(
                parent_pid=parent_pid,
                peer_credentials=peer,
                read_process_facts=failing_callback,
            )
        self.assertEqual(str(rejected.exception), "onefile Local API canary failed")
        self.assertNotIn("PRIVATE", str(rejected.exception))
        self.assertEqual(callback_calls, [parent_pid])
        self.assertEqual(hostile_comparisons, [0])

    def test_read_linux_process_facts_binds_one_stable_bounded_proc_snapshot(self):
        import scripts.canary_onefile_local_api as canary
        from scripts.canary_onefile_local_api import (
            OnefileLocalAPICanaryError,
            OnefileProcessFacts,
            read_linux_process_facts,
        )

        pid = 4312
        ppid = 4300
        current_uid = os.getuid()
        current_gid = os.getgid()
        cgroup = (
            "0::/user.slice/"
            f"user-{current_uid}.slice/user@{current_uid}.service/"
            "app.slice/openusage-bar.service\n"
        ).encode("ascii")
        argv = b"/PRIVATE/openusage-collector\0daemon\0"
        executable = SimpleNamespace(
            st_mode=0o100700,
            st_uid=current_uid,
            st_gid=current_gid,
            st_nlink=1,
            st_dev=71,
            st_ino=81,
            st_size=91,
            st_mtime_ns=101,
            st_ctime_ns=102,
        )
        expected = OnefileProcessFacts(
            pid=pid,
            uid=current_uid,
            gid=current_gid,
            ppid=ppid,
            start_time_ticks=2000,
            cgroup=cgroup.decode("ascii"),
            executable_dev=71,
            executable_ino=81,
            executable_size_bytes=91,
            executable_mode=0o100700,
            executable_mtime_ns=101,
            executable_ctime_ns=102,
            executable_nlink=1,
            argv_nul=argv,
        )

        def stat_payload(start_time):
            remaining = ["S", str(ppid)] + ["0"] * 17 + [str(start_time)]
            return f"{pid} (openusage-collector) {' '.join(remaining)}\n".encode(
                "ascii"
            )

        status = (
            f"Name:\tcollector\nUid:\t{current_uid}\t{current_uid}\t"
            f"{current_uid}\t{current_uid}\nGid:\t{current_gid}\t"
            f"{current_gid}\t{current_gid}\t{current_gid}\n"
        ).encode("ascii")
        expected_order = [
            "status",
            "stat",
            "cgroup",
            "exe",
            "cmdline",
            "status",
            "stat",
            "cgroup",
            "exe",
            "cmdline",
            "status",
            "stat",
        ]

        def exercise(start_times, *, expected_error):
            proc_descriptor = 7100
            pid_descriptor = 7101
            next_descriptor = [7200]
            descriptor_payloads = {}
            read_counts = {}
            opened_files = []
            closed = []
            stat_values = iter(start_times)
            payloads = {
                "status": iter((status, status, status)),
                "stat": (stat_payload(value) for value in stat_values),
                "cgroup": iter((cgroup, cgroup)),
                "cmdline": iter((argv, argv)),
            }

            def open_path(path, flags, *args, dir_fd=None, **kwargs):
                self.assertEqual(args, ())
                self.assertEqual(kwargs, {})
                if path == "/proc":
                    self.assertIsNone(dir_fd)
                    self.assertEqual(flags, 0x10000 | 0x20000 | 0x80000)
                    return proc_descriptor
                if path == str(pid):
                    self.assertEqual(dir_fd, proc_descriptor)
                    self.assertEqual(flags, 0x10000 | 0x20000 | 0x80000)
                    return pid_descriptor
                self.assertIn(path, {"status", "stat", "cgroup", "cmdline"})
                self.assertEqual(dir_fd, pid_descriptor)
                self.assertEqual(flags, 0x20000 | 0x80000)
                descriptor = next_descriptor[0]
                next_descriptor[0] += 1
                descriptor_payloads[descriptor] = next(payloads[path])
                read_counts[descriptor] = 0
                opened_files.append(path)
                return descriptor

            directory_metadata = {
                proc_descriptor: SimpleNamespace(
                    st_mode=0o40555,
                    st_uid=0,
                    st_gid=0,
                    st_nlink=1,
                    st_dev=1,
                    st_ino=2,
                ),
                pid_descriptor: SimpleNamespace(
                    st_mode=0o40555,
                    st_uid=current_uid,
                    st_gid=current_gid,
                    st_nlink=1,
                    st_dev=1,
                    st_ino=3,
                ),
            }

            def fstat_descriptor(descriptor):
                if descriptor in directory_metadata:
                    return directory_metadata[descriptor]
                payload = descriptor_payloads[descriptor]
                return SimpleNamespace(
                    st_mode=0o100400,
                    st_uid=current_uid,
                    st_gid=current_gid,
                    st_nlink=1,
                    st_dev=1,
                    st_ino=descriptor,
                    st_size=len(payload),
                    st_mtime_ns=1,
                    st_ctime_ns=1,
                )

            def read_descriptor(descriptor, size):
                self.assertGreater(size, 0)
                self.assertLessEqual(size, 65_537)
                count = read_counts[descriptor]
                read_counts[descriptor] += 1
                return descriptor_payloads[descriptor] if count == 0 else b""

            def stat_executable(path, *args, dir_fd=None, follow_symlinks=True, **kwargs):
                self.assertEqual(path, "exe")
                self.assertEqual(args, ())
                self.assertEqual(kwargs, {})
                self.assertEqual(dir_fd, pid_descriptor)
                self.assertIs(follow_symlinks, True)
                opened_files.append("exe")
                return executable

            def close_descriptor(descriptor):
                self.assertNotIn(descriptor, closed)
                closed.append(descriptor)

            with ExitStack() as stack:
                stack.enter_context(patch.object(canary.sys, "platform", "linux"))
                stack.enter_context(patch.object(canary.os, "O_DIRECTORY", 0x10000))
                stack.enter_context(patch.object(canary.os, "O_NOFOLLOW", 0x20000))
                stack.enter_context(patch.object(canary.os, "O_CLOEXEC", 0x80000))
                stack.enter_context(patch.object(canary.os, "O_RDONLY", 0))
                stack.enter_context(patch.object(canary.os, "open", side_effect=open_path))
                stack.enter_context(patch.object(canary.os, "fstat", side_effect=fstat_descriptor))
                stack.enter_context(patch.object(canary.os, "read", side_effect=read_descriptor))
                stack.enter_context(patch.object(canary.os, "stat", side_effect=stat_executable))
                stack.enter_context(patch.object(canary.os, "close", side_effect=close_descriptor))
                if expected_error:
                    with self.assertRaisesRegex(
                        OnefileLocalAPICanaryError,
                        "onefile Local API canary failed",
                    ) as rejected:
                        read_linux_process_facts(pid)
                    self.assertEqual(
                        str(rejected.exception),
                        "onefile Local API canary failed",
                    )
                    self.assertNotIn("PRIVATE", str(rejected.exception))
                else:
                    self.assertEqual(read_linux_process_facts(pid), expected)

            self.assertEqual(opened_files, expected_order)
            self.assertEqual(closed[-2:], [pid_descriptor, proc_descriptor])
            self.assertEqual(len(closed), len(set(closed)))

        exercise((2000, 2000, 2000), expected_error=False)
        exercise((2000, 2001, 2001), expected_error=True)

    def test_run_onefile_local_api_canary_owns_happy_process_and_cleanup(self):
        import scripts.canary_onefile_local_api as canary
        from scripts.canary_onefile_local_api import (
            OnefileLocalAPICanaryError,
            OnefileLocalAPISummary,
            OnefileProcessFacts,
            run_onefile_local_api_canary,
        )
        from openusage_bar.shared_client_boundary import (
            SharedClientBoundaryAttemptCounters,
        )

        with tempfile.TemporaryDirectory(prefix="canary-test-", dir="/tmp") as outer:
            root = os.path.abspath(outer)
            collector = os.path.join(root, "openusage-collector")
            with open(collector, "wb") as output:
                output.write(b"PRIVATE-audited-onefile")
            os.chmod(collector, 0o700)
            collector_before = os.lstat(collector)
            run_root = os.path.join(root, "owned-run")
            os.mkdir(run_root, 0o700)
            socket_path = os.path.join(run_root, "openusage.sock")
            isolated_directories = {
                "HOME": os.path.join(run_root, "home"),
                "XDG_DATA_HOME": os.path.join(run_root, "data"),
                "XDG_CONFIG_HOME": os.path.join(run_root, "config"),
                "XDG_CACHE_HOME": os.path.join(run_root, "cache"),
                "TMPDIR": os.path.join(run_root, "tmp"),
            }
            expected_environment = {
                **isolated_directories,
                "LANG": "C",
                "LC_ALL": "C",
                "PYTHONNOUSERSITE": "1",
            }
            parent_pid = 4300
            child_pid = 4312
            peer = struct.pack("=3i", child_pid, os.getuid(), os.getgid())
            cgroup = (
                "0::/user.slice/"
                f"user-{os.getuid()}.slice/user@{os.getuid()}.service/"
                "app.slice/openusage-bar.service\n"
            )

            def process_facts(*, pid, ppid, start_time):
                return OnefileProcessFacts(
                    pid=pid,
                    uid=os.getuid(),
                    gid=os.getgid(),
                    ppid=ppid,
                    start_time_ticks=start_time,
                    cgroup=cgroup,
                    executable_dev=collector_before.st_dev,
                    executable_ino=collector_before.st_ino,
                    executable_size_bytes=collector_before.st_size,
                    executable_mode=collector_before.st_mode,
                    executable_mtime_ns=collector_before.st_mtime_ns,
                    executable_ctime_ns=collector_before.st_ctime_ns,
                    executable_nlink=collector_before.st_nlink,
                    argv_nul=b"/audited/openusage-collector\0daemon\0",
                )

            parent_facts = process_facts(pid=parent_pid, ppid=1, start_time=1000)
            child_facts = process_facts(
                pid=child_pid,
                ppid=parent_pid,
                start_time=1001,
            )
            payload = (
                b'{"schemaVersion":"1.0","dataRevision":7,'
                b'"generatedAt":"2026-08-12T00:00:00Z","sources":[],'
                b'"health":{"ok":true,"status":"ok"}}'
            )
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + payload
            )
            chunks = iter((response[:29], response[29:], b""))
            expected_request = (
                b"GET /v1/health HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept: application/json\r\n"
                b"Connection: close\r\n\r\n"
            )
            events = []
            servers = []
            terminated = [False]
            launched_environments = []
            isolated_directory_facts = []
            inherited_environment_accesses = []

            class HostileEnvironment(dict):
                def _reject(self, *_args, **_kwargs):
                    inherited_environment_accesses.append("access")
                    raise RuntimeError("PRIVATE inherited environment")

                __contains__ = _reject
                __getitem__ = _reject
                __iter__ = _reject
                copy = _reject
                get = _reject
                items = _reject
                keys = _reject
                values = _reject

            class Process:
                pid = parent_pid

                def __init__(self):
                    self.wait_calls = 0

                def poll(self):
                    events.append("poll")
                    return None

                def wait(self, *, timeout):
                    self_outer.assertTrue(terminated[0])
                    self_outer.assertGreater(timeout, 0.0)
                    self_outer.assertLessEqual(timeout, 2.0)
                    self.wait_calls += 1
                    events.append(f"wait{self.wait_calls}")
                    if self.wait_calls == 1:
                        raise subprocess.TimeoutExpired(
                            (collector,), timeout
                        )
                    self_outer.assertEqual(self.wait_calls, 2)
                    return -9

            process = Process()
            real_socket = socket.socket
            original_environment = canary.os.environ

            def launch(argv, **kwargs):
                self.assertEqual(
                    argv,
                    (
                        collector,
                        "--offline",
                        "daemon",
                        "--interval",
                        "60",
                        "--api-transport",
                        "unix",
                        "--api-socket",
                        socket_path,
                    ),
                )
                base_kwargs = {
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL,
                    "start_new_session": True,
                }
                self.assertEqual(
                    {key: kwargs[key] for key in base_kwargs},
                    base_kwargs,
                )
                self.assertEqual(set(kwargs) - {"env"}, set(base_kwargs))
                environment = kwargs.get("env")
                launched_environments.append(environment)
                if type(environment) is dict and environment == expected_environment:
                    for path in isolated_directories.values():
                        metadata = os.lstat(path)
                        isolated_directory_facts.append(
                            (
                                path,
                                stat.S_ISDIR(metadata.st_mode),
                                stat.S_IMODE(metadata.st_mode),
                                metadata.st_uid,
                                metadata.st_gid,
                            )
                        )
                canary.os.environ = original_environment
                server = real_socket(socket.AF_UNIX, socket.SOCK_STREAM)
                servers.append(server)
                server.bind(socket_path)
                os.chmod(socket_path, 0o600)
                events.append("popen")
                return process

            class Client:
                def __init__(self):
                    self.closed = False
                    self.peer_reads = 0

                def settimeout(self, timeout):
                    self_outer.assertGreater(timeout, 0.0)
                    self_outer.assertLessEqual(timeout, 1.0)

                def connect(self, address):
                    self_outer.assertEqual(address, socket_path)
                    events.append("connect")

                def getsockopt(self, level, option, size):
                    self_outer.assertEqual((level, option, size), (1, 17, 12))
                    self.peer_reads += 1
                    events.append(f"peer{self.peer_reads}")
                    return peer

                def sendall(self, request):
                    self_outer.assertEqual(request, expected_request)
                    events.append("send")

                def recv(self, size):
                    self_outer.assertEqual(size, 65_536)
                    chunk = next(chunks)
                    events.append(f"recv{len(chunk)}")
                    return chunk

                def close(self):
                    self_outer.assertFalse(self.closed)
                    self.closed = True
                    events.append("client_close")

            self_outer = self
            client = Client()
            clock = [100.0]

            def monotonic():
                value = clock[0]
                clock[0] += 0.05
                return value

            expected_fact_reads = iter(
                (
                    ("parent_before", parent_pid, parent_facts),
                    ("child_before", child_pid, child_facts),
                    ("child_after", child_pid, child_facts),
                    ("parent_after", parent_pid, parent_facts),
                )
            )

            def read_process_fact(pid):
                label, expected_pid, facts = next(expected_fact_reads)
                self.assertEqual(pid, expected_pid)
                events.append(label)
                return facts

            def evaluate(**kwargs):
                self.assertEqual(kwargs["parent_pid"], parent_pid)
                self.assertEqual(kwargs["peer_credentials"], peer)
                cached_reader = kwargs["read_process_facts"]
                self.assertIsNot(cached_reader, process_reader)
                self.assertEqual(cached_reader(parent_pid), parent_facts)
                self.assertEqual(cached_reader(child_pid), child_facts)
                self.assertEqual(cached_reader(child_pid), child_facts)
                self.assertEqual(cached_reader(parent_pid), parent_facts)
                events.append("evaluate")
                return OnefileLocalAPISummary(True)

            boundary_fact = SharedClientBoundaryAttemptCounters("a" * 64, 0, 0)

            def read_boundary(path, *, remaining_timeout):
                self.assertEqual(path, socket_path)
                self.assertGreater(remaining_timeout(), 0.0)
                events.append("boundary")
                return peer, boundary_fact

            def terminate_group(pid, selected_signal):
                self.assertEqual(pid, parent_pid)
                if selected_signal == signal.SIGTERM:
                    terminated[0] = True
                    events.append("term")
                    return None
                if selected_signal == signal.SIGKILL:
                    events.append("kill")
                    return None
                self.assertEqual(selected_signal, 0)
                if "kill" not in events:
                    events.append("probe_alive")
                    return None
                events.append("probe_gone")
                raise ProcessLookupError

            real_rmtree = canary.shutil.rmtree

            def remove_run_root(path):
                self.assertEqual(path, run_root + ".owned-cleanup")
                events.append("remove")
                return real_rmtree(path)

            try:
                with ExitStack() as stack:
                    stack.enter_context(patch.object(canary.sys, "platform", "linux"))
                    stack.enter_context(
                        patch.object(canary.platform, "machine", return_value="x86_64")
                    )
                    stack.enter_context(
                        patch.object(canary.tempfile, "mkdtemp", return_value=run_root)
                    )
                    popen = stack.enter_context(
                        patch.object(canary.subprocess, "Popen", side_effect=launch)
                    )
                    socket_factory = stack.enter_context(
                        patch.object(canary.socket, "socket", return_value=client)
                    )
                    stack.enter_context(
                        patch.object(canary.time, "monotonic", side_effect=monotonic)
                    )
                    stack.enter_context(
                        patch.object(canary.os, "environ", HostileEnvironment())
                    )
                    process_reader = stack.enter_context(
                        patch.object(
                            canary,
                            "read_linux_process_facts",
                            side_effect=read_process_fact,
                        )
                    )
                    evaluator = stack.enter_context(
                        patch.object(
                            canary,
                            "evaluate_onefile_local_api_peer",
                            side_effect=evaluate,
                        )
                    )
                    boundary_reader = stack.enter_context(
                        patch.object(
                            canary,
                            "read_onefile_shared_client_boundary_snapshot",
                            side_effect=read_boundary,
                        )
                    )
                    term = stack.enter_context(
                        patch.object(canary.os, "killpg", side_effect=terminate_group)
                    )
                    kill = stack.enter_context(patch.object(canary.os, "kill"))
                    stack.enter_context(
                        patch.object(
                            canary.shutil,
                            "rmtree",
                            side_effect=remove_run_root,
                        )
                    )

                    cleanup_error = None
                    summary = None
                    try:
                        summary = run_onefile_local_api_canary(collector)
                    except OnefileLocalAPICanaryError as error:
                        cleanup_error = error
                        self.assertEqual(inherited_environment_accesses, [])

                popen.assert_called_once()
                socket_factory.assert_called_once_with(
                    socket.AF_UNIX,
                    socket.SOCK_STREAM,
                )
                evaluator.assert_called_once()
                self.assertEqual(boundary_reader.call_count, 2)
                self.assertEqual(launched_environments, [expected_environment])
                self.assertEqual(
                    isolated_directory_facts,
                    [
                        (path, True, 0o700, os.getuid(), os.getgid())
                        for path in isolated_directories.values()
                    ],
                )
                self.assertEqual(
                    process_reader.call_args_list,
                    [
                        ((parent_pid,), {}),
                        ((child_pid,), {}),
                        ((child_pid,), {}),
                        ((parent_pid,), {}),
                    ],
                )
                self.assertEqual(
                    term.call_args_list,
                    [
                        ((parent_pid, signal.SIGTERM), {}),
                        ((parent_pid, 0), {}),
                        ((parent_pid, signal.SIGKILL), {}),
                        ((parent_pid, 0), {}),
                    ],
                )
                kill.assert_not_called()
                self.assertTrue(client.closed)
                self.assertEqual(client.peer_reads, 2)
                collector_after = os.lstat(collector)
                self.assertEqual(
                    (
                        collector_after.st_dev,
                        collector_after.st_ino,
                        collector_after.st_mode,
                        collector_after.st_size,
                    ),
                    (
                        collector_before.st_dev,
                        collector_before.st_ino,
                        collector_before.st_mode,
                        collector_before.st_size,
                    ),
                )
                self.assertEqual(
                    events,
                    [
                        "popen",
                        "connect",
                        "peer1",
                        "parent_before",
                        "child_before",
                        "boundary",
                        "send",
                        f"recv{29}",
                        f"recv{len(response) - 29}",
                        "recv0",
                        "peer2",
                        "boundary",
                        "child_after",
                        "parent_after",
                        "evaluate",
                        "client_close",
                        "poll",
                        "term",
                        "wait1",
                        "probe_alive",
                        "kill",
                        "wait2",
                        "probe_gone",
                        "remove",
                    ],
                )
                self.assertIsNone(cleanup_error)
                self.assertEqual(summary, OnefileLocalAPISummary(True))
                self.assertFalse(os.path.lexists(run_root))
            finally:
                for server in servers:
                    server.close()

    def test_run_onefile_local_api_canary_closes_each_failed_connection_candidate(self):
        import scripts.canary_onefile_local_api as canary
        from scripts.canary_onefile_local_api import (
            OnefileLocalAPICanaryError,
            run_onefile_local_api_canary,
        )

        cases = (
            ("clock regresses before connect", (100.0, 99.0), None, None),
        )
        for name, clock_values, connect_errno, exit_code in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory(
                    prefix="canary-failure-test-",
                    dir="/tmp",
                ) as outer:
                    collector = os.path.join(outer, "openusage-collector")
                    with open(collector, "wb") as output:
                        output.write(b"PRIVATE-audited-onefile")
                    os.chmod(collector, 0o700)
                    run_root = os.path.join(outer, "owned-run")
                    os.mkdir(run_root, 0o700)
                    parent_pid = 4300
                    client_closed = []
                    waited = []

                    class Process:
                        pid = parent_pid

                        def poll(self):
                            return exit_code

                        def wait(self, *, timeout):
                            self_outer.assertEqual(timeout, 2.0)
                            waited.append(timeout)
                            return 0

                    class Client:
                        def settimeout(self, timeout):
                            self_outer.assertGreater(timeout, 0.0)

                        def connect(self, _address):
                            if connect_errno is not None:
                                raise OSError(connect_errno, "PRIVATE connect failure")
                            self_outer.fail("clock regression must fail before connect")

                        def close(self):
                            client_closed.append(True)

                    self_outer = self
                    client = Client()
                    clock = iter(clock_values)

                    def group_gone(pid, selected_signal):
                        self.assertEqual(pid, parent_pid)
                        if selected_signal == signal.SIGTERM:
                            return None
                        self.assertEqual(selected_signal, 0)
                        raise ProcessLookupError

                    with ExitStack() as stack:
                        stack.enter_context(patch.object(canary.sys, "platform", "linux"))
                        stack.enter_context(
                            patch.object(canary.platform, "machine", return_value="x86_64")
                        )
                        stack.enter_context(
                            patch.object(canary.tempfile, "mkdtemp", return_value=run_root)
                        )
                        stack.enter_context(
                            patch.object(canary.subprocess, "Popen", return_value=Process())
                        )
                        stack.enter_context(
                            patch.object(canary.socket, "socket", return_value=client)
                        )
                        stack.enter_context(
                            patch.object(canary.time, "monotonic", side_effect=clock)
                        )
                        terminate = stack.enter_context(
                            patch.object(canary.os, "killpg", side_effect=group_gone)
                        )
                        with self.assertRaisesRegex(
                            OnefileLocalAPICanaryError,
                            "onefile Local API canary failed",
                        ) as rejected:
                            run_onefile_local_api_canary(collector)

                    self.assertEqual(
                        str(rejected.exception),
                        "onefile Local API canary failed",
                    )
                    self.assertNotIn("PRIVATE", str(rejected.exception))
                    self.assertEqual(client_closed, [True])
                    self.assertFalse(os.path.lexists(run_root))
                    if exit_code is None:
                        self.assertEqual(
                            terminate.call_args_list,
                            [
                                ((parent_pid, signal.SIGTERM), {}),
                                ((parent_pid, 0), {}),
                            ],
                        )
                        self.assertEqual(waited, [2.0])
                    else:
                        terminate.assert_not_called()
                        self.assertEqual(waited, [])

    def test_run_onefile_local_api_canary_cleans_the_owned_group_after_leader_exit(self):
        import scripts.canary_onefile_local_api as canary
        from scripts.canary_onefile_local_api import (
            OnefileLocalAPICanaryError,
            run_onefile_local_api_canary,
        )

        with tempfile.TemporaryDirectory(
            prefix="canary-process-group-test-",
            dir="/tmp",
        ) as outer:
            collector = os.path.join(outer, "openusage-collector")
            with open(collector, "wb") as output:
                output.write(b"PRIVATE-audited-onefile")
            os.chmod(collector, 0o700)
            run_root = os.path.join(outer, "owned-run")
            os.mkdir(run_root, 0o700)
            parent_pid = 4300
            events = []
            probe_count = [0]

            class Process:
                pid = parent_pid

                def poll(self):
                    events.append("leader_exited")
                    return 7

                def wait(self, *, timeout):
                    self_outer.assertGreater(timeout, 0.0)
                    self_outer.assertLessEqual(timeout, 2.0)
                    events.append("wait_leader")
                    return 7

            class Client:
                def settimeout(self, timeout):
                    self_outer.assertGreater(timeout, 0.0)

                def connect(self, _address):
                    raise OSError(errno.ENOENT, "PRIVATE socket unavailable")

                def close(self):
                    events.append("candidate_close")

            self_outer = self

            def group_signal(pid, selected_signal):
                self.assertEqual(pid, parent_pid)
                if selected_signal == signal.SIGTERM:
                    events.append("term_group")
                    return None
                if selected_signal == signal.SIGKILL:
                    events.append("kill_group")
                    return None
                self.assertEqual(selected_signal, 0)
                probe_count[0] += 1
                if probe_count[0] == 1:
                    events.append("probe_group_alive")
                    return None
                events.append("probe_group_gone")
                raise ProcessLookupError

            real_rmtree = canary.shutil.rmtree

            def remove_run_root(path):
                self.assertEqual(path, run_root + ".owned-cleanup")
                events.append("remove_run_root")
                return real_rmtree(path)

            clock = [100.0]

            def monotonic():
                value = clock[0]
                clock[0] += 0.05
                return value

            with ExitStack() as stack:
                stack.enter_context(patch.object(canary.sys, "platform", "linux"))
                stack.enter_context(
                    patch.object(canary.platform, "machine", return_value="x86_64")
                )
                stack.enter_context(
                    patch.object(canary.tempfile, "mkdtemp", return_value=run_root)
                )
                stack.enter_context(
                    patch.object(canary.subprocess, "Popen", return_value=Process())
                )
                stack.enter_context(
                    patch.object(canary.socket, "socket", return_value=Client())
                )
                stack.enter_context(
                    patch.object(canary.time, "monotonic", side_effect=monotonic)
                )
                stack.enter_context(patch.object(canary.time, "sleep"))
                stack.enter_context(
                    patch.object(canary.os, "killpg", side_effect=group_signal)
                )
                stack.enter_context(
                    patch.object(canary.shutil, "rmtree", side_effect=remove_run_root)
                )
                with self.assertRaisesRegex(
                    OnefileLocalAPICanaryError,
                    "onefile Local API canary failed",
                ) as rejected:
                    run_onefile_local_api_canary(collector)

            self.assertEqual(
                str(rejected.exception),
                "onefile Local API canary failed",
            )
            self.assertNotIn("PRIVATE", str(rejected.exception))
            self.assertEqual(events.count("candidate_close"), 1)
            self.assertGreaterEqual(events.count("leader_exited"), 2)
            group_cleanup_events = [
                event
                for event in events
                if event
                in {
                    "term_group",
                    "wait_leader",
                    "probe_group_alive",
                    "kill_group",
                    "probe_group_gone",
                    "remove_run_root",
                }
            ]
            self.assertEqual(
                group_cleanup_events,
                [
                    "term_group",
                    "wait_leader",
                    "probe_group_alive",
                    "kill_group",
                    "probe_group_gone",
                    "remove_run_root",
                ],
            )
            self.assertFalse(os.path.lexists(run_root))

    def test_run_onefile_local_api_canary_reaps_the_leader_after_a_gone_group_probe(self):
        import scripts.canary_onefile_local_api as canary
        from openusage_bar.shared_client_boundary import (
            SharedClientBoundaryAttemptCounters,
        )
        from scripts.canary_onefile_local_api import (
            OnefileLocalAPICanaryError,
            OnefileLocalAPISummary,
            OnefileProcessFacts,
            run_onefile_local_api_canary,
        )

        with tempfile.TemporaryDirectory(prefix="canary-gone-probe-", dir="/tmp") as outer:
            collector = os.path.join(outer, "openusage-collector")
            with open(collector, "wb") as output:
                output.write(b"audited onefile collector")
            os.chmod(collector, 0o700)
            run_root = os.path.join(outer, "owned-run")
            os.mkdir(run_root, 0o700)
            socket_path = os.path.join(run_root, "openusage.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(socket_path)
            os.chmod(socket_path, 0o600)
            parent_pid = 4300
            child_pid = 4312
            events: list[str] = []
            collector_metadata = os.lstat(collector)
            cgroup = (
                "0::/user.slice/"
                f"user-{os.getuid()}.slice/user@{os.getuid()}.service/"
                "app.slice/openusage-bar.service\n"
            )

            def process_facts(*, pid, ppid, start_time):
                return OnefileProcessFacts(
                    pid=pid,
                    uid=os.getuid(),
                    gid=os.getgid(),
                    ppid=ppid,
                    start_time_ticks=start_time,
                    cgroup=cgroup,
                    executable_dev=collector_metadata.st_dev,
                    executable_ino=collector_metadata.st_ino,
                    executable_size_bytes=collector_metadata.st_size,
                    executable_mode=collector_metadata.st_mode,
                    executable_mtime_ns=collector_metadata.st_mtime_ns,
                    executable_ctime_ns=collector_metadata.st_ctime_ns,
                    executable_nlink=collector_metadata.st_nlink,
                    argv_nul=b"/audited/openusage-collector\0daemon\0",
                )

            parent_facts = process_facts(
                pid=parent_pid,
                ppid=1,
                start_time=1000,
            )
            child_facts = process_facts(
                pid=child_pid,
                ppid=parent_pid,
                start_time=1001,
            )
            fact_reads = iter(
                (parent_facts, child_facts, child_facts, parent_facts)
            )

            class Process:
                pid = parent_pid

                def __init__(self):
                    self.wait_calls = 0

                def poll(self):
                    return None

                def wait(self, *, timeout):
                    self_outer.assertGreater(timeout, 0.0)
                    self_outer.assertLessEqual(timeout, 2.0)
                    self.wait_calls += 1
                    events.append(f"wait{self.wait_calls}")
                    if self.wait_calls == 1:
                        raise subprocess.TimeoutExpired((collector,), timeout)
                    self_outer.assertEqual(self.wait_calls, 2)
                    return -15

            process = Process()
            body = json.dumps(
                {
                    "schemaVersion": "1.0",
                    "dataRevision": 0,
                    "generatedAt": "2026-08-12T00:00:00Z",
                    "sources": [],
                    "health": {"ok": True, "status": "ok"},
                },
                separators=(",", ":"),
            ).encode("utf-8")
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + body
            )

            class Client:
                def __init__(self):
                    self.peer_reads = 0
                    self.chunks = iter((response, b""))

                def settimeout(self, timeout):
                    self_outer.assertGreater(timeout, 0.0)

                def connect(self, _address):
                    return None

                def getsockopt(self, level, option, size):
                    self_outer.assertEqual((level, option, size), (1, 17, 12))
                    self.peer_reads += 1
                    return struct.pack("=3i", child_pid, os.getuid(), os.getgid())

                def sendall(self, request):
                    self_outer.assertIn(b"GET /v1/health", request)

                def recv(self, size):
                    self_outer.assertEqual(size, 65_536)
                    return next(self.chunks)

                def close(self):
                    pass

            self_outer = self

            def group_signal(pid, selected_signal):
                self.assertEqual(pid, parent_pid)
                if selected_signal == signal.SIGTERM:
                    events.append("term")
                    return None
                self.assertEqual(selected_signal, 0)
                events.append("probe_gone")
                raise ProcessLookupError

            real_rmtree = canary.shutil.rmtree

            def remove_run_root(path):
                self.assertEqual(path, run_root + ".owned-cleanup")
                events.append("remove")
                return real_rmtree(path)

            clock = [100.0]

            def monotonic():
                value = clock[0]
                clock[0] += 0.05
                return value

            def evaluate_gone_probe(
                *, parent_pid, peer_credentials, read_process_facts
            ):
                self.assertEqual(parent_pid, process.pid)
                self.assertEqual(
                    peer_credentials,
                    struct.pack("=3i", child_pid, os.getuid(), os.getgid()),
                )
                self.assertEqual(read_process_facts(parent_pid), parent_facts)
                self.assertEqual(read_process_facts(child_pid), child_facts)
                self.assertEqual(read_process_facts(child_pid), child_facts)
                self.assertEqual(read_process_facts(parent_pid), parent_facts)
                return OnefileLocalAPISummary(True)

            with ExitStack() as stack:
                stack.enter_context(patch.object(canary.sys, "platform", "linux"))
                stack.enter_context(
                    patch.object(canary.platform, "machine", return_value="x86_64")
                )
                stack.enter_context(
                    patch.object(canary.tempfile, "mkdtemp", return_value=run_root)
                )
                stack.enter_context(
                    patch.object(canary.subprocess, "Popen", return_value=process)
                )
                stack.enter_context(
                    patch.object(canary.socket, "socket", return_value=Client())
                )
                stack.enter_context(
                    patch.object(canary.time, "monotonic", side_effect=monotonic)
                )
                stack.enter_context(patch.object(canary.time, "sleep"))
                stack.enter_context(
                    patch.object(canary.os, "killpg", side_effect=group_signal)
                )
                stack.enter_context(
                    patch.object(canary.shutil, "rmtree", side_effect=remove_run_root)
                )
                stack.enter_context(
                    patch.object(
                        canary,
                        "evaluate_onefile_local_api_peer",
                        side_effect=evaluate_gone_probe,
                    )
                )
                stack.enter_context(
                    patch.object(
                        canary,
                        "read_linux_process_facts",
                        side_effect=fact_reads,
                    )
                )
                stack.enter_context(
                    patch.object(
                        canary,
                        "read_onefile_shared_client_boundary_snapshot",
                        return_value=(
                            struct.pack(
                                "=3i", child_pid, os.getuid(), os.getgid()
                            ),
                            SharedClientBoundaryAttemptCounters("a" * 64, 0, 0),
                        ),
                    )
                )
                cleanup_error = None
                try:
                    summary = run_onefile_local_api_canary(collector)
                except OnefileLocalAPICanaryError as error:
                    cleanup_error = error
                    summary = None
            server.close()

            self.assertEqual(
                events,
                ["term", "wait1", "probe_gone", "wait2", "remove"],
            )
            self.assertIsNone(cleanup_error)
            self.assertEqual(summary, OnefileLocalAPISummary(True))
            self.assertFalse(os.path.lexists(run_root))

    def test_run_onefile_local_api_canary_never_deletes_a_swapped_run_root(self):
        import scripts.canary_onefile_local_api as canary
        from scripts.canary_onefile_local_api import (
            OnefileLocalAPICanaryError,
            run_onefile_local_api_canary,
        )

        with tempfile.TemporaryDirectory(
            prefix="canary-run-root-swap-test-",
            dir="/tmp",
        ) as outer:
            collector = os.path.join(outer, "openusage-collector")
            with open(collector, "wb") as output:
                output.write(b"PRIVATE-audited-onefile")
            os.chmod(collector, 0o700)
            collector_before = os.lstat(collector)
            run_root = os.path.join(outer, "owned-run")
            os.mkdir(run_root, 0o700)
            owned_marker = os.path.join(run_root, "owned.marker")
            with open(owned_marker, "wb") as output:
                output.write(b"owned")
            original = os.path.join(outer, "owned-original")
            foreign_marker = os.path.join(run_root, "PRIVATE-foreign.marker")
            parent_pid = 4300
            client_closed = []
            swapped = []
            foreign_identity = []

            class Process:
                pid = parent_pid

                def poll(self):
                    return None

                def wait(self, *, timeout):
                    self_outer.assertEqual(timeout, 2.0)
                    return 0

            class Client:
                def close(self):
                    client_closed.append(True)

            self_outer = self
            real_rmtree = canary.shutil.rmtree

            def swap_before_delete(path, *args, **kwargs):
                self.assertFalse(swapped)
                if os.path.lexists(run_root):
                    os.rename(run_root, original)
                os.mkdir(run_root, 0o700)
                with open(foreign_marker, "wb") as output:
                    output.write(b"PRIVATE foreign bytes")
                metadata = os.lstat(run_root)
                foreign_identity.append((metadata.st_dev, metadata.st_ino))
                swapped.append(os.fspath(path))
                return real_rmtree(path, *args, **kwargs)

            def group_gone(pid, selected_signal):
                self.assertEqual(pid, parent_pid)
                if selected_signal == signal.SIGTERM:
                    return None
                self.assertEqual(selected_signal, 0)
                raise ProcessLookupError

            clock = iter((100.0, 99.0))
            with ExitStack() as stack:
                stack.enter_context(patch.object(canary.sys, "platform", "linux"))
                stack.enter_context(
                    patch.object(canary.platform, "machine", return_value="x86_64")
                )
                stack.enter_context(
                    patch.object(canary.tempfile, "mkdtemp", return_value=run_root)
                )
                stack.enter_context(
                    patch.object(canary.subprocess, "Popen", return_value=Process())
                )
                stack.enter_context(
                    patch.object(canary.socket, "socket", return_value=Client())
                )
                stack.enter_context(
                    patch.object(canary.time, "monotonic", side_effect=clock)
                )
                stack.enter_context(
                    patch.object(canary.os, "killpg", side_effect=group_gone)
                )
                stack.enter_context(
                    patch.object(canary.shutil, "rmtree", side_effect=swap_before_delete)
                )
                with self.assertRaisesRegex(
                    OnefileLocalAPICanaryError,
                    "onefile Local API canary failed",
                ) as rejected:
                    run_onefile_local_api_canary(collector)

            self.assertEqual(swapped, [swapped[0]])
            self.assertEqual(client_closed, [True])
            self.assertEqual(
                str(rejected.exception),
                "onefile Local API canary failed",
            )
            self.assertNotIn("PRIVATE", str(rejected.exception))
            self.assertTrue(os.path.isdir(run_root))
            with open(foreign_marker, "rb") as input_file:
                self.assertEqual(input_file.read(), b"PRIVATE foreign bytes")
            foreign_after = os.lstat(run_root)
            self.assertEqual(
                (foreign_after.st_dev, foreign_after.st_ino),
                foreign_identity[0],
            )
            if os.path.lexists(original):
                with open(os.path.join(original, "owned.marker"), "rb") as input_file:
                    self.assertEqual(input_file.read(), b"owned")
            collector_after = os.lstat(collector)
            self.assertEqual(
                (
                    collector_after.st_dev,
                    collector_after.st_ino,
                    collector_after.st_size,
                    collector_after.st_mode,
                ),
                (
                    collector_before.st_dev,
                    collector_before.st_ino,
                    collector_before.st_size,
                    collector_before.st_mode,
                ),
            )

    def test_run_onefile_local_api_canary_preserves_root_when_group_is_uncertain(self):
        import scripts.canary_onefile_local_api as canary
        from scripts.canary_onefile_local_api import (
            OnefileLocalAPICanaryError,
            run_onefile_local_api_canary,
        )

        with tempfile.TemporaryDirectory(
            prefix="canary-uncertain-group-test-",
            dir="/tmp",
        ) as outer:
            collector = os.path.join(outer, "openusage-collector")
            with open(collector, "wb") as output:
                output.write(b"PRIVATE-audited-onefile")
            os.chmod(collector, 0o700)
            run_root = os.path.join(outer, "owned-run")
            os.mkdir(run_root, 0o700)
            marker = os.path.join(run_root, "PRIVATE-owned.marker")
            with open(marker, "wb") as output:
                output.write(b"owned bytes")
            root_before = os.lstat(run_root)
            parent_pid = 4300
            client_closed = []

            class Process:
                pid = parent_pid

                def poll(self):
                    return None

                def wait(self, *, timeout):
                    self_outer.assertEqual(timeout, 2.0)
                    return 0

            class Client:
                def close(self):
                    client_closed.append(True)

            self_outer = self

            def uncertain_group(pid, selected_signal):
                self.assertEqual(pid, parent_pid)
                if selected_signal in {signal.SIGTERM, signal.SIGKILL}:
                    return None
                self.assertEqual(selected_signal, 0)
                raise PermissionError("PRIVATE group probe denied")

            clock = iter((100.0, 99.0, 101.0))
            with ExitStack() as stack:
                stack.enter_context(patch.object(canary.sys, "platform", "linux"))
                stack.enter_context(
                    patch.object(canary.platform, "machine", return_value="x86_64")
                )
                stack.enter_context(
                    patch.object(canary.tempfile, "mkdtemp", return_value=run_root)
                )
                stack.enter_context(
                    patch.object(canary.subprocess, "Popen", return_value=Process())
                )
                stack.enter_context(
                    patch.object(canary.socket, "socket", return_value=Client())
                )
                stack.enter_context(
                    patch.object(canary.time, "monotonic", side_effect=clock)
                )
                stack.enter_context(
                    patch.object(canary.os, "killpg", side_effect=uncertain_group)
                )
                rename = stack.enter_context(patch.object(canary.os, "rename"))
                remove = stack.enter_context(patch.object(canary.shutil, "rmtree"))
                with self.assertRaisesRegex(
                    OnefileLocalAPICanaryError,
                    "onefile Local API canary failed",
                ) as rejected:
                    run_onefile_local_api_canary(collector)

            self.assertEqual(
                str(rejected.exception),
                "onefile Local API canary failed",
            )
            self.assertNotIn("PRIVATE", str(rejected.exception))
            rename.assert_not_called()
            remove.assert_not_called()
            self.assertEqual(client_closed, [True])
            root_after = os.lstat(run_root)
            self.assertEqual(
                (root_after.st_dev, root_after.st_ino),
                (root_before.st_dev, root_before.st_ino),
            )
            with open(marker, "rb") as input_file:
                self.assertEqual(input_file.read(), b"owned bytes")

    def test_run_onefile_local_api_canary_binds_process_executable_to_collector(self):
        import scripts.canary_onefile_local_api as canary
        from openusage_bar.shared_client_boundary import (
            SharedClientBoundaryAttemptCounters,
        )
        from scripts.canary_onefile_local_api import (
            OnefileLocalAPICanaryError,
            OnefileLocalAPISummary,
            OnefileProcessFacts,
            run_onefile_local_api_canary,
        )

        with tempfile.TemporaryDirectory(
            prefix="canary-artifact-test-",
            dir="/tmp",
        ) as outer:
            collector = os.path.join(outer, "openusage-collector")
            with open(collector, "wb") as output:
                output.write(b"PRIVATE-audited-onefile")
            os.chmod(collector, 0o700)
            collector_before = os.lstat(collector)
            run_root = os.path.join(outer, "owned-run")
            os.mkdir(run_root, 0o700)
            socket_path = os.path.join(run_root, "openusage.sock")
            parent_pid = 4300
            child_pid = 4312
            peer = struct.pack("=3i", child_pid, os.getuid(), os.getgid())
            cgroup = (
                "0::/user.slice/"
                f"user-{os.getuid()}.slice/user@{os.getuid()}.service/"
                "app.slice/openusage-bar.service\n"
            )

            def facts(pid):
                return OnefileProcessFacts(
                    pid=pid,
                    uid=os.getuid(),
                    gid=os.getgid(),
                    ppid=1 if pid == parent_pid else parent_pid,
                    start_time_ticks=1000 if pid == parent_pid else 1001,
                    cgroup=cgroup,
                    executable_dev=collector_before.st_dev,
                    executable_ino=collector_before.st_ino + 1,
                    executable_size_bytes=collector_before.st_size,
                    executable_mode=collector_before.st_mode,
                    executable_mtime_ns=collector_before.st_mtime_ns,
                    executable_ctime_ns=collector_before.st_ctime_ns,
                    executable_nlink=collector_before.st_nlink,
                    argv_nul=b"/PRIVATE/foreign-onefile\0daemon\0",
                )

            payload = (
                b'{"schemaVersion":"1.0","dataRevision":7,'
                b'"generatedAt":"2026-08-12T00:00:00Z","sources":[],'
                b'"health":{"ok":true,"status":"ok"}}'
            )
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + payload
            )
            chunks = iter((response, b""))
            servers = []
            client_closed = []

            class Process:
                pid = parent_pid

                def poll(self):
                    return None

                def wait(self, *, timeout):
                    self_outer.assertEqual(timeout, 2.0)
                    return 0

            class Client:
                def settimeout(self, timeout):
                    self_outer.assertGreater(timeout, 0.0)

                def connect(self, address):
                    self_outer.assertEqual(address, socket_path)

                def getsockopt(self, level, option, size):
                    self_outer.assertEqual((level, option, size), (1, 17, 12))
                    return peer

                def sendall(self, _request):
                    return None

                def recv(self, size):
                    self_outer.assertEqual(size, 65_536)
                    return next(chunks)

                def close(self):
                    client_closed.append(True)

            self_outer = self
            real_socket = socket.socket

            def launch(_argv, **_kwargs):
                server = real_socket(socket.AF_UNIX, socket.SOCK_STREAM)
                servers.append(server)
                server.bind(socket_path)
                os.chmod(socket_path, 0o600)
                return Process()

            clock = [100.0]

            def monotonic():
                value = clock[0]
                clock[0] += 0.05
                return value

            def evaluate_foreign_facts(**kwargs):
                cached_reader = kwargs["read_process_facts"]
                self.assertEqual(cached_reader(parent_pid), facts(parent_pid))
                self.assertEqual(cached_reader(child_pid), facts(child_pid))
                self.assertEqual(cached_reader(child_pid), facts(child_pid))
                self.assertEqual(cached_reader(parent_pid), facts(parent_pid))
                return OnefileLocalAPISummary(True)

            def group_gone(pid, selected_signal):
                self.assertEqual(pid, parent_pid)
                if selected_signal == signal.SIGTERM:
                    return None
                self.assertEqual(selected_signal, 0)
                raise ProcessLookupError

            try:
                with ExitStack() as stack:
                    stack.enter_context(patch.object(canary.sys, "platform", "linux"))
                    stack.enter_context(
                        patch.object(canary.platform, "machine", return_value="x86_64")
                    )
                    stack.enter_context(
                        patch.object(canary.tempfile, "mkdtemp", return_value=run_root)
                    )
                    stack.enter_context(
                        patch.object(canary.subprocess, "Popen", side_effect=launch)
                    )
                    stack.enter_context(
                        patch.object(canary.socket, "socket", return_value=Client())
                    )
                    stack.enter_context(
                        patch.object(canary.time, "monotonic", side_effect=monotonic)
                    )
                    process_reader = stack.enter_context(
                        patch.object(
                            canary,
                            "read_linux_process_facts",
                            side_effect=facts,
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            canary,
                            "read_onefile_shared_client_boundary_snapshot",
                            return_value=(
                                peer,
                                SharedClientBoundaryAttemptCounters(
                                    "a" * 64, 0, 0
                                ),
                            ),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            canary,
                            "evaluate_onefile_local_api_peer",
                            side_effect=evaluate_foreign_facts,
                        )
                    )
                    stack.enter_context(
                        patch.object(canary.os, "killpg", side_effect=group_gone)
                    )

                    with self.assertRaisesRegex(
                        OnefileLocalAPICanaryError,
                        "onefile Local API canary failed",
                    ) as rejected:
                        run_onefile_local_api_canary(collector)

                self.assertEqual(
                    str(rejected.exception),
                    "onefile Local API canary failed",
                )
                self.assertNotIn("PRIVATE", str(rejected.exception))
                self.assertEqual(
                    process_reader.call_args_list,
                    [
                        ((parent_pid,), {}),
                        ((child_pid,), {}),
                        ((child_pid,), {}),
                        ((parent_pid,), {}),
                    ],
                )
                self.assertEqual(client_closed, [True])
                self.assertFalse(os.path.lexists(run_root))
                collector_after = os.lstat(collector)
                self.assertEqual(
                    (
                        collector_after.st_dev,
                        collector_after.st_ino,
                        collector_after.st_size,
                        collector_after.st_mode,
                        collector_after.st_mtime_ns,
                        collector_after.st_ctime_ns,
                        collector_after.st_nlink,
                    ),
                    (
                        collector_before.st_dev,
                        collector_before.st_ino,
                        collector_before.st_size,
                        collector_before.st_mode,
                        collector_before.st_mtime_ns,
                        collector_before.st_ctime_ns,
                        collector_before.st_nlink,
                    ),
                )
            finally:
                for server in servers:
                    server.close()

    def test_main_emits_only_the_canonical_closed_summary(self):
        import scripts.canary_onefile_local_api as canary
        from scripts.canary_onefile_local_api import (
            OnefileLocalAPISummary,
            main,
        )

        collector = "/PRIVATE/audited/openusage-collector"

        def invoke(arguments):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(canary.sys, "stdout", stdout),
                patch.object(canary.sys, "stderr", stderr),
            ):
                result = main(arguments)
            return result, stdout.getvalue(), stderr.getvalue()

        with patch.object(
            canary,
            "run_onefile_local_api_canary",
            return_value=OnefileLocalAPISummary(True),
        ) as runner:
            self.assertEqual(
                invoke(("--collector", collector)),
                (0, '{"stableDirectChild":true}\n', ""),
            )
            runner.assert_called_once_with(collector)

        with patch.object(
            canary,
            "run_onefile_local_api_canary",
            side_effect=RuntimeError("PRIVATE runner failure " + collector),
        ) as runner:
            self.assertEqual(
                invoke(("--collector", collector)),
                (1, "", "onefile_local_api_canary_failed\n"),
            )
            runner.assert_called_once_with(collector)

        for arguments in (
            (),
            ("--collector",),
            ("--collector", "relative/PRIVATE-collector"),
            ("--collector", collector, "--PRIVATE-unknown"),
        ):
            with self.subTest(arguments=arguments):
                with patch.object(canary, "run_onefile_local_api_canary") as runner:
                    self.assertEqual(
                        invoke(arguments),
                        (2, "", "onefile_local_api_canary_failed\n"),
                    )
                    runner.assert_not_called()

    def test_main_normalizes_hostile_argument_iteration(self):
        import scripts.canary_onefile_local_api as canary
        from scripts.canary_onefile_local_api import main

        private_value = "PRIVATE hostile CLI argument iterator"

        class HostileArguments(list):
            def __iter__(self):
                raise RuntimeError(private_value)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(canary.sys, "stdout", stdout),
            patch.object(canary.sys, "stderr", stderr),
            patch.object(canary, "run_onefile_local_api_canary") as runner,
        ):
            result = main(HostileArguments(("--collector", "/PRIVATE/collector")))

        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "onefile_local_api_canary_failed\n")
        self.assertNotIn(private_value, stdout.getvalue() + stderr.getvalue())
        runner.assert_not_called()

    def test_direct_execution_enters_main_only_after_all_helpers_are_defined(self):
        script = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "scripts",
                "canary_onefile_local_api.py",
            )
        )
        with open(script, encoding="utf-8") as input_file:
            source_lines = input_file.read().splitlines()
        guard_line = source_lines.index('if __name__ == "__main__":') + 1
        required_helpers = {
            "evaluate_onefile_local_api_peer",
            "read_linux_process_facts",
            "run_onefile_local_api_canary",
            "main",
            "_collector_signature",
            "_directory_identity",
            "_read_process_identity",
            "_parse_status_identity",
            "_read_process_executable",
            "_read_proc_text",
            "_read_proc_bytes",
            "_executable_signature",
        }
        guard_facts = []

        def trace_guard(frame, event, _argument):
            if (
                event == "line"
                and os.path.abspath(frame.f_code.co_filename) == script
                and frame.f_lineno == guard_line
            ):
                guard_facts.append(required_helpers - set(frame.f_globals))
            return trace_guard

        stdout = io.StringIO()
        stderr = io.StringIO()
        previous_trace = sys.gettrace()
        try:
            sys.settrace(trace_guard)
            with (
                patch.object(sys, "argv", [script]),
                patch.object(sys, "stdout", stdout),
                patch.object(sys, "stderr", stderr),
            ):
                with self.assertRaises(SystemExit) as exited:
                    runpy.run_path(script, run_name="__main__")
        finally:
            sys.settrace(previous_trace)

        self.assertEqual(exited.exception.code, 2)
        self.assertEqual(guard_facts, [set()])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "onefile_local_api_canary_failed\n")

    def test_module_entrypoint_preserves_project_package_discovery(self):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        module = "scripts.canary_onefile_local_api"
        closed_environment = {
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": root,
        }

        entrypoint = subprocess.run(
            (sys.executable, "-m", module),
            cwd=root,
            env=closed_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(entrypoint.returncode, 2)
        self.assertEqual(entrypoint.stdout, "")
        self.assertEqual(entrypoint.stderr, "onefile_local_api_canary_failed\n")

        discovery = subprocess.run(
            (
                sys.executable,
                "-S",
                "-c",
                "import scripts.canary_onefile_local_api as canary; "
                "from openusage_bar.local_api import "
                "_parse_local_api_health_response; "
                "assert callable(canary.main); "
                "assert callable(_parse_local_api_health_response); "
                "print('onefile_canary_module_discovery_ok=1')",
            ),
            cwd="/tmp",
            env=closed_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(discovery.returncode, 0)
        self.assertEqual(discovery.stdout, "onefile_canary_module_discovery_ok=1\n")
        self.assertEqual(discovery.stderr, "")


if __name__ == "__main__":
    unittest.main()
