from __future__ import annotations

import http.client
import hashlib
import io
import json
import os
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import patch

import openusage_bar.local_api as local_api_module
from openusage_bar.activity_store import (
    ActivityStore,
    DailyCostRow,
    DailyUsageRow,
    ProviderInstance,
    QuotaObservation,
)
from openusage_bar.capabilities import (
    AccountScope,
    CapabilityState,
    ModelScope,
    OperatingSystem,
    ProviderRegistry,
    QuotaWindow,
    QuotaWindowCapability,
    SourceAuthority,
    SourceFactFamily,
    SourceProvenance,
    SourceStability,
    SourceVerification,
    registry,
)
from openusage_bar.local_api import create_tcp_server, create_unix_server
from openusage_bar.collector_cli import main as collector_main
from openusage_bar.provider_catalog import ObserverPlatformResolver, catalog
from openusage_bar.query import QueryService


NOW = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
TOKEN = "t" * 48


def seeded_query() -> tuple[ActivityStore, QueryService]:
    store = ActivityStore(":memory:")
    store.replace_daily_usage("codex", "2026-07-14", [DailyUsageRow(
        day="2026-07-14", provider_id="codex", model_id="gpt-5.5",
        input_tokens=60, output_tokens=20, cache_read_tokens=20,
        cache_creation_tokens=0, reasoning_tokens=None, total_tokens=100,
        cost_amount=None, cost_currency=None, cost_basis=None, quality="direct",
        imported_at="2026-07-14T09:00:00Z",
    )])
    store.replace_daily_costs("openai", "2026-07-14", [DailyCostRow(
        day="2026-07-14", provider_id="openai", cost_kind="actual",
        currency="USD", amount="12.34", basis="provider_reported",
        quality="direct", imported_at="2026-07-14T09:00:00Z",
    )])
    store.record_quota(QuotaObservation(
        record_id="minimax.five_hour", observed_at="2026-07-14T09:00:00Z",
        provider_id="minimax", quota_name="Five hour", unit="percent",
        used="82", quota_limit="100", remaining="18", remaining_ratio=0.18,
        resets_at="2026-07-14T12:00:00Z", period_start=None, period_end=None,
        state="ok", quality="direct", stale=False,
    ))
    store.record_source_success("minimax", "current.quota", NOW)
    store.upsert_provider_instance(ProviderInstance(
        provider_id="minimax-primary", family_id="minimax",
        display_name="MiniMax primary", category="subscription",
        credential_source="minimax_builtin_api", source_kind="builtin_api",
        observed_at="2026-07-14T09:00:00Z",
    ))
    store.upsert_provider_instance(ProviderInstance(
        provider_id="zfuture", family_id="zfuture",
        display_name="Future Provider", category="api",
        credential_source="openusage", source_kind="openusage",
        observed_at="2026-07-14T09:01:00Z",
    ))
    return store, QueryService(store, clock=lambda: NOW)


def start(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _capture_error(action, errors):
    try:
        action()
    except Exception as error:
        errors.append(error)


def unix_request(path: Path, target: str, *, method: str = "GET", headers=None, body=b""):
    headers = {"Host": "localhost", **(headers or {})}
    lines = [f"{method} {target} HTTP/1.1", *(f"{k}: {v}" for k, v in headers.items()), "", ""]
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(str(path))
    client.sendall("\r\n".join(lines).encode("ascii") + body)
    response = http.client.HTTPResponse(client)
    response._method = method
    response.begin()
    payload = response.read()
    result = response.status, {k.lower(): v for k, v in response.getheaders()}, payload
    client.close()
    return result


def unix_raw_request(path: Path, request: bytes):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(str(path))
    client.sendall(request)
    response = http.client.HTTPResponse(client)
    response.begin()
    result = response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    client.close()
    return result


def raw_exchange(address, request: bytes) -> bytes:
    if isinstance(address, (str, Path)):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(str(address))
    else:
        client = socket.create_connection(address, timeout=2)
    client.settimeout(2)
    try:
        client.sendall(request)
        chunks = []
        while True:
            try:
                chunk = client.recv(65_536)
            except (ConnectionResetError, TimeoutError):
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        client.close()


def split_raw_response(response: bytes):
    head, body = response.split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    headers = {}
    for line in lines[1:]:
        name, value = line.split(b":", 1)
        headers[name.decode("ascii").lower()] = value.strip().decode("ascii")
    return lines[0], headers, body


class _LinuxObservationClient:
    def __init__(self, response: bytes, peer_credentials: bytes):
        self._chunks = iter((response[:31], response[31:], b""))
        self._peer_credentials = peer_credentials
        self.connected = 0
        self.peer_reads = 0
        self.request_count = 0
        self.eof_reads = 0
        self.closed = False

    def settimeout(self, timeout):
        assert type(timeout) is float and 0.0 < timeout <= 1.0

    def connect(self, address):
        assert isinstance(address, str)
        assert address.startswith("/proc/self/fd/")
        assert address.endswith("/openusage.sock")
        self.connected += 1

    def getsockopt(self, level, option, size):
        assert (level, option, size) == (1, 17, 12)
        self.peer_reads += 1
        return self._peer_credentials

    def sendall(self, request):
        assert request == (
            b"GET /v1/health HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Accept: application/json\r\n"
            b"Connection: close\r\n\r\n"
        )
        self.request_count += 1

    def recv(self, size):
        assert size == 65_536
        chunk = next(self._chunks)
        if not chunk:
            self.eof_reads += 1
        return chunk

    def close(self):
        assert not self.closed
        self.closed = True


@contextmanager
def _stable_linux_local_api_peer_process(test_case, *, pid=4312):
    from types import SimpleNamespace

    process = local_api_module._LinuxLocalAPIPeerProcessFact(
        uid=os.getuid(),
        gid=os.getgid(),
        parent_pid=4300,
        start_time_ticks=987655,
        executable_path="/tmp/audited/openusage-collector",
        executable_signature=(
            91,
            92,
            stat.S_IFREG | 0o700,
            os.getuid(),
            os.getgid(),
            1,
            1024,
            101,
            102,
        ),
        argv_nul=b"/tmp/audited/openusage-collector\0daemon\0",
        cgroup=b"0::/user.slice/openusage-bar.service\n",
    )
    proc_descriptor = 93_000
    peer_descriptor = 93_001
    real_open = local_api_module.os.open
    real_fstat = local_api_module.os.fstat
    real_close = local_api_module.os.close

    def open_entry(path, flags, *args, dir_fd=None, **kwargs):
        if path == "/proc":
            test_case.assertEqual(args, ())
            test_case.assertEqual(kwargs, {})
            test_case.assertIsNone(dir_fd)
            test_case.assertTrue(flags & os.O_DIRECTORY)
            test_case.assertTrue(flags & os.O_NOFOLLOW)
            return proc_descriptor
        if (os.fspath(path), dir_fd) == (str(pid), proc_descriptor):
            test_case.assertEqual(args, ())
            test_case.assertEqual(kwargs, {})
            test_case.assertTrue(flags & os.O_DIRECTORY)
            test_case.assertTrue(flags & os.O_NOFOLLOW)
            return peer_descriptor
        if dir_fd is None:
            return real_open(path, flags, *args, **kwargs)
        return real_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

    def fstat_entry(descriptor):
        if descriptor == proc_descriptor:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o555, st_uid=0)
        if descriptor == peer_descriptor:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o555,
                st_uid=os.getuid(),
                st_gid=os.getgid(),
            )
        return real_fstat(descriptor)

    synthetic_closed = []

    def close_entry(descriptor):
        if descriptor in {proc_descriptor, peer_descriptor}:
            test_case.assertNotIn(descriptor, synthetic_closed)
            synthetic_closed.append(descriptor)
            return None
        return real_close(descriptor)

    with (
        patch.object(local_api_module.os, "open", side_effect=open_entry),
        patch.object(local_api_module.os, "fstat", side_effect=fstat_entry),
        patch.object(local_api_module.os, "close", side_effect=close_entry),
        patch.object(
            local_api_module,
            "_read_linux_local_api_peer_process",
            side_effect=(process, process),
        ) as process_reader,
    ):
        yield SimpleNamespace(
            process=process,
            reader=process_reader,
            descriptors=(proc_descriptor, peer_descriptor),
            closed=synthetic_closed,
        )


_COMPLETE_HEALTH_PAYLOAD = {
    "schemaVersion": "1.0",
    "dataRevision": 7,
    "generatedAt": "2026-08-12T00:00:00Z",
    "sources": [],
    "health": {"ok": True, "status": "ok"},
}


def _health_response(payload=None) -> bytes:
    body = json.dumps(
        _COMPLETE_HEALTH_PAYLOAD if payload is None else payload,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json; charset=utf-8\r\n"
        + f"Content-Length: {len(body)}\r\n".encode("ascii")
        + b"Connection: close\r\n\r\n"
        + body
    )


class _LinuxLocalAPIObservationHarness:
    def __init__(self, test_case, response=None, *, marker=b"unchanged"):
        import struct

        from openusage_bar.lifecycle_state import LifecycleStatePaths

        self.test_case = test_case
        self.temporary = tempfile.TemporaryDirectory(prefix="lua-", dir="/tmp")
        self.root = Path(self.temporary.name)
        self.home = self.root / "authoritative-home"
        self.state_parent = self.home / ".local" / "state"
        self.state_root = self.state_parent / "openusage-bar"
        self.state_root.mkdir(parents=True, mode=0o700)
        self.state_root.chmod(0o700)
        self.marker = self.state_root / "observation-marker"
        self.marker.write_bytes(marker)
        self.marker_bytes = marker
        self.socket_path = self.state_root / "openusage.sock"
        self.real_socket = socket.socket
        self.servers = []
        self.add_socket(self.socket_path)
        self.socket_identity = self.identity(self.socket_path)
        self.authority = LifecycleStatePaths(platform="linux", home=self.home)
        self.response = _health_response() if response is None else response
        self.peer_credentials = struct.pack(
            "=3i",
            4312,
            os.getuid(),
            os.getgid(),
        )
        self.client = _LinuxObservationClient(
            self.response,
            self.peer_credentials,
        )
        self.clock = [100.0]
        self.stat_side_effect = None
        self.stack = None
        self.socket_factory = None
        self.peer_process = None

    @staticmethod
    def identity(path):
        metadata = path.lstat()
        return metadata.st_dev, metadata.st_ino

    def add_socket(self, path):
        server = self.real_socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.servers.append(server)
        server.bind(str(path))
        path.chmod(0o600)
        return server

    def monotonic(self):
        value = self.clock[0]
        self.clock[0] += 0.05
        return value

    def __enter__(self):
        from contextlib import ExitStack

        from openusage_bar.lifecycle_state import LifecycleStatePaths

        self.stack = ExitStack()
        self.stack.enter_context(
            patch.object(local_api_module.sys, "platform", "linux")
        )
        self.stack.enter_context(
            patch.object(
                LifecycleStatePaths,
                "for_current_user",
                return_value=self.authority,
            )
        )
        self.stack.enter_context(
            patch.object(local_api_module.Path, "home", return_value=self.home)
        )
        if self.stat_side_effect is not None:
            self.stack.enter_context(
                patch.object(
                    local_api_module.os,
                    "stat",
                    side_effect=self.stat_side_effect,
                )
            )
        self.socket_factory = self.stack.enter_context(
            patch.object(
                local_api_module.socket,
                "socket",
                return_value=self.client,
            )
        )
        self.stack.enter_context(
            patch.object(
                local_api_module.time,
                "monotonic",
                side_effect=self.monotonic,
            )
        )
        self.peer_process = self.stack.enter_context(
            _stable_linux_local_api_peer_process(self.test_case)
        )
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.stack is not None:
            self.stack.close()
        for server in self.servers:
            server.close()
        self.temporary.cleanup()

    def assert_full_transaction(self):
        self.test_case.assertEqual(
            (
                self.client.connected,
                self.client.peer_reads,
                self.client.request_count,
                self.client.eof_reads,
            ),
            (1, 2, 1, 1),
        )
        self.test_case.assertTrue(self.client.closed)

    def assert_marker_unchanged(self):
        self.test_case.assertEqual(self.marker.read_bytes(), self.marker_bytes)


@unittest.skipIf(os.name == "nt", "requires native Linux descriptor semantics")
class LinuxLocalAPIObservationTests(unittest.TestCase):
    def test_current_user_boundary_reader_uses_canonical_socket_and_one_deadline(
        self,
    ):
        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from openusage_bar.local_api import (
            LinuxSharedClientBoundaryState,
            read_current_user_shared_client_boundary_state,
        )

        home = Path("/authoritative-home")
        authority = LifecycleStatePaths(platform="linux", home=home)
        expected = LinuxSharedClientBoundaryState(
            peer_pid=4312,
            peer_uid=os.getuid(),
            peer_gid=os.getgid(),
            process_epoch_sha256="a" * 64,
            bounded_http_open_attempts=0,
            headless_keychain_get_attempts=0,
        )
        clock = iter((100.0, 100.25, 101.75))

        def read_boundary(path, *, remaining_timeout):
            self.assertEqual(
                path,
                "/authoritative-home/.local/state/openusage-bar/openusage.sock",
            )
            self.assertEqual(remaining_timeout(), 1.0)
            self.assertEqual(remaining_timeout(), 0.25)
            return expected

        with patch.object(
            LifecycleStatePaths,
            "for_current_user",
            return_value=authority,
        ) as current_user, patch(
            "openusage_bar.local_api.sys.platform",
            "linux",
        ), patch(
            "openusage_bar.local_api.Path.home",
            return_value=home,
        ), patch(
            "openusage_bar.local_api.time.monotonic",
            side_effect=lambda: next(clock),
        ), patch(
            "openusage_bar.local_api.read_linux_shared_client_boundary_state",
            side_effect=read_boundary,
        ) as raw_reader:
            self.assertIs(
                read_current_user_shared_client_boundary_state(),
                expected,
            )

        current_user.assert_called_once_with(platform="linux")
        raw_reader.assert_called_once()

    def test_read_current_user_local_api_state_binds_socket_peer_and_health(self):
        import struct
        from contextlib import ExitStack
        from types import SimpleNamespace

        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from openusage_bar.local_api import (
            LinuxLocalAPIState,
            read_current_user_local_api_state,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            state_root = home / ".local" / "state" / "openusage-bar"
            state_root.mkdir(parents=True, mode=0o700)
            state_root.chmod(0o700)
            marker = state_root / "PRIVATE-observation-marker"
            marker.write_bytes(b"do-not-read-or-modify")
            authority = LifecycleStatePaths(platform="linux", home=home)
            socket_path = state_root / "openusage.sock"
            current_uid = os.getuid()
            current_gid = os.getgid()
            peer_pid = 4312
            peer_parent_pid = 4300
            peer_start_time_ticks = 987655
            peer_executable = root / "audited" / "openusage-collector"
            peer_argv_nul = (
                os.fsencode(peer_executable)
                + b"\0daemon\0--interval\0"
                + b"300\0"
            )
            peer_cgroup = (
                "0::/user.slice/"
                f"user-{current_uid}.slice/user@{current_uid}.service/"
                "app.slice/openusage-bar.service\n"
            )
            directory_paths = (
                home,
                home / ".local",
                home / ".local" / "state",
                state_root,
            )
            directory_facts = tuple(path.lstat() for path in directory_paths)
            self.assertEqual(stat.S_IMODE(directory_facts[-1].st_mode), 0o700)
            self.assertEqual(directory_facts[-1].st_uid, current_uid)
            before = tuple(
                (path.relative_to(root).as_posix(), path.lstat().st_mode, path.read_bytes())
                for path in sorted(root.rglob("*"))
                if path.is_file()
            )

            socket_fact = SimpleNamespace(
                st_mode=stat.S_IFSOCK | 0o600,
                st_uid=current_uid,
                st_gid=current_gid,
                st_nlink=1,
                st_dev=71,
                st_ino=81,
                st_size=0,
                st_mtime_ns=91,
                st_ctime_ns=92,
            )
            descriptors = (7100, 7101, 7102, 7103)
            descriptor_facts = dict(zip(descriptors, directory_facts, strict=True))
            proc_descriptor = 7200
            peer_descriptor = 7201
            proc_file_descriptors = tuple(range(7202, 7210))
            proc_directory_facts = {
                proc_descriptor: SimpleNamespace(
                    st_mode=stat.S_IFDIR | 0o555,
                    st_uid=0,
                    st_gid=0,
                    st_nlink=1,
                ),
                peer_descriptor: SimpleNamespace(
                    st_mode=stat.S_IFDIR | 0o555,
                    st_uid=current_uid,
                    st_gid=current_gid,
                    st_nlink=1,
                ),
            }
            proc_file_fact = SimpleNamespace(
                st_mode=stat.S_IFREG | 0o400,
                st_uid=current_uid,
                st_gid=current_gid,
                st_nlink=1,
                st_size=128,
            )
            peer_executable_fact = SimpleNamespace(
                st_mode=stat.S_IFREG | 0o700,
                st_uid=current_uid,
                st_gid=current_gid,
                st_nlink=1,
                st_dev=91,
                st_ino=92,
                st_size=1024,
                st_mtime_ns=101,
                st_ctime_ns=102,
            )
            peer_status = (
                f"Name:\tcollector\nUid:\t{current_uid}\t{current_uid}\t"
                f"{current_uid}\t{current_uid}\nGid:\t{current_gid}\t"
                f"{current_gid}\t{current_gid}\t{current_gid}\n"
            ).encode("ascii")
            peer_stat = (
                f"{peer_pid} (openusage-collector) S {peer_parent_pid} "
                + " ".join("0" for _ in range(17))
                + f" {peer_start_time_ticks} 0\n"
            ).encode("ascii")
            proc_snapshots = (
                ("before", "status", proc_file_descriptors[0], peer_status),
                ("before", "stat", proc_file_descriptors[1], peer_stat),
                (
                    "before",
                    "cgroup",
                    proc_file_descriptors[2],
                    peer_cgroup.encode("ascii"),
                ),
                ("before", "cmdline", proc_file_descriptors[3], peer_argv_nul),
                ("after", "status", proc_file_descriptors[4], peer_status),
                ("after", "stat", proc_file_descriptors[5], peer_stat),
                (
                    "after",
                    "cgroup",
                    proc_file_descriptors[6],
                    peer_cgroup.encode("ascii"),
                ),
                ("after", "cmdline", proc_file_descriptors[7], peer_argv_nul),
            )
            proc_open_expectations = iter(proc_snapshots)
            proc_payloads = {
                descriptor: iter((payload, b""))
                for _phase, _name, descriptor, payload in proc_snapshots
            }
            child_facts = {
                (descriptors[0], ".local"): directory_facts[1],
                (descriptors[1], "state"): directory_facts[2],
                (descriptors[2], "openusage-bar"): directory_facts[3],
            }
            open_expectations = iter(
                (
                    (home, None, descriptors[0]),
                    (".local", descriptors[0], descriptors[1]),
                    ("state", descriptors[1], descriptors[2]),
                    ("openusage-bar", descriptors[2], descriptors[3]),
                )
            )
            events: list[tuple[object, ...]] = []

            def open_directory(path, flags, *args, dir_fd=None, **kwargs):
                self.assertEqual(args, ())
                self.assertEqual(kwargs, {})
                name = os.fspath(path)
                if name in {"status", "stat", "cgroup", "cmdline"}:
                    phase, expected_name, descriptor, _payload = next(
                        proc_open_expectations
                    )
                    self.assertEqual((name, dir_fd), (expected_name, peer_descriptor))
                    self.assertTrue(flags & os.O_NOFOLLOW)
                    self.assertFalse(flags & os.O_DIRECTORY)
                    events.append(("proc_open", phase, name, descriptor))
                    return descriptor
                if path == "/proc":
                    self.assertIsNone(dir_fd)
                    self.assertTrue(flags & os.O_DIRECTORY)
                    self.assertTrue(flags & os.O_NOFOLLOW)
                    events.append(("proc_directory", proc_descriptor))
                    return proc_descriptor
                if (name, dir_fd) == (str(peer_pid), proc_descriptor):
                    self.assertTrue(flags & os.O_DIRECTORY)
                    self.assertTrue(flags & os.O_NOFOLLOW)
                    events.append(("peer_directory", peer_descriptor))
                    return peer_descriptor
                expected_path, expected_parent, descriptor = next(open_expectations)
                self.assertEqual(path, expected_path)
                self.assertEqual(dir_fd, expected_parent)
                self.assertTrue(flags & os.O_DIRECTORY)
                self.assertTrue(flags & os.O_NOFOLLOW)
                events.append(("open", os.fspath(path), dir_fd, descriptor))
                return descriptor

            def fstat_directory(descriptor):
                events.append(("fstat", descriptor))
                if descriptor in descriptor_facts:
                    return descriptor_facts[descriptor]
                if descriptor in proc_directory_facts:
                    return proc_directory_facts[descriptor]
                self.assertIn(descriptor, proc_file_descriptors)
                return proc_file_fact

            def read_proc_file(descriptor, size):
                self.assertIn(descriptor, proc_file_descriptors)
                self.assertIs(type(size), int)
                self.assertGreater(size, 0)
                self.assertLessEqual(size, 65_537)
                payload = next(proc_payloads[descriptor])
                events.append(("proc_read", descriptor, len(payload)))
                return payload

            executable_reads = 0

            def read_peer_executable(path, *args, dir_fd=None, **kwargs):
                nonlocal executable_reads
                self.assertEqual(args, ())
                self.assertEqual(kwargs, {})
                self.assertEqual((path, dir_fd), ("exe", peer_descriptor))
                executable_reads += 1
                events.append(("proc_exe_path", executable_reads))
                return os.fspath(peer_executable)

            home_stats = 0
            held_socket_stats = 0
            canonical_socket_stats = 0

            def stat_canonical_socket(selected_home):
                nonlocal canonical_socket_stats
                self.assertEqual(selected_home, home)
                canonical_socket_stats += 1
                events.append(("canonical_socket_stat", canonical_socket_stats))
                return socket_fact

            def stat_entry(path, *args, dir_fd=None, follow_symlinks=True, **kwargs):
                nonlocal home_stats, held_socket_stats
                self.assertEqual(args, ())
                self.assertEqual(kwargs, {})
                name = os.fspath(path)
                if (dir_fd, name) == (peer_descriptor, "exe"):
                    self.assertIs(follow_symlinks, True)
                    events.append(("proc_exe_stat", executable_reads))
                    return peer_executable_fact
                self.assertIs(follow_symlinks, False)
                if dir_fd is None:
                    self.assertEqual(path, home)
                    home_stats += 1
                    events.append(("home_stat", home_stats))
                    return directory_facts[0]
                if (dir_fd, name) in child_facts:
                    events.append(("revalidate", dir_fd, name))
                    return child_facts[(dir_fd, name)]
                self.assertEqual((dir_fd, name), (descriptors[3], "openusage.sock"))
                held_socket_stats += 1
                events.append(("held_socket_stat", held_socket_stats))
                return socket_fact

            closed_descriptors: list[int] = []

            def close_descriptor(descriptor):
                self.assertIn(
                    descriptor,
                    descriptors
                    + (proc_descriptor, peer_descriptor)
                    + proc_file_descriptors,
                )
                self.assertNotIn(descriptor, closed_descriptors)
                closed_descriptors.append(descriptor)
                events.append(("close_fd", descriptor))

            payload = json.dumps(
                {
                    "schemaVersion": "1.0",
                    "dataRevision": 7,
                    "generatedAt": "2026-08-12T00:00:00Z",
                    "sources": [],
                    "health": {"ok": True, "status": "ok"},
                },
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + payload
            )
            response_chunks = iter(
                (response[:19], response[19:73], response[73:], b"")
            )
            request = (
                b"GET /v1/health HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept: application/json\r\n"
                b"Connection: close\r\n\r\n"
            )
            peer_credentials = struct.pack("=3i", peer_pid, current_uid, current_gid)
            clock_value = [100.0]
            observed_times: list[float] = []
            timeouts: list[float] = []

            def monotonic():
                value = clock_value[0]
                observed_times.append(value)
                clock_value[0] += 0.05
                return value

            class FakeSocket:
                def __init__(self):
                    self.closed = False
                    self.peer_reads = 0

                def settimeout(self, timeout):
                    self.assert_open()
                    self_outer.assertIs(type(timeout), float)
                    self_outer.assertGreater(timeout, 0.0)
                    self_outer.assertLessEqual(timeout, 2.0)
                    timeouts.append(timeout)
                    events.append(("timeout", timeout))

                def connect(self, address):
                    self.assert_open()
                    self_outer.assertEqual(
                        address,
                        f"/proc/self/fd/{descriptors[3]}/openusage.sock",
                    )
                    events.append(("connect", address))

                def getsockopt(self, level, option, length):
                    self.assert_open()
                    self_outer.assertEqual((level, option, length), (1, 17, 12))
                    self.peer_reads += 1
                    events.append(("peer", self.peer_reads))
                    return peer_credentials

                def sendall(self, value):
                    self.assert_open()
                    self_outer.assertEqual(value, request)
                    events.append(("sendall", value))

                def recv(self, size):
                    self.assert_open()
                    self_outer.assertIs(type(size), int)
                    self_outer.assertGreater(size, 0)
                    chunk = next(response_chunks)
                    events.append(("recv", len(chunk)))
                    return chunk

                def close(self):
                    self_outer.assertFalse(self.closed)
                    self.closed = True
                    events.append(("socket_close",))

                def assert_open(self):
                    self_outer.assertFalse(self.closed)

            self_outer = self
            fake_socket = FakeSocket()

            def create_socket(family, kind, *args, **kwargs):
                self.assertEqual(args, ())
                self.assertEqual(kwargs, {})
                self.assertEqual((family, kind), (socket.AF_UNIX, socket.SOCK_STREAM))
                events.append(("socket", family, kind))
                return fake_socket

            with ExitStack() as stack:
                stack.enter_context(patch.object(local_api_module.sys, "platform", "linux"))
                authority_reader = stack.enter_context(
                    patch.object(
                        LifecycleStatePaths,
                        "for_current_user",
                        return_value=authority,
                    )
                )
                stack.enter_context(patch.object(local_api_module.Path, "home", return_value=home))
                stack.enter_context(patch.object(local_api_module.os, "open", side_effect=open_directory))
                stack.enter_context(patch.object(local_api_module.os, "fstat", side_effect=fstat_directory))
                stack.enter_context(patch.object(local_api_module.os, "read", side_effect=read_proc_file))
                stack.enter_context(
                    patch.object(
                        local_api_module.os,
                        "readlink",
                        side_effect=read_peer_executable,
                    )
                )
                stack.enter_context(patch.object(local_api_module.os, "stat", side_effect=stat_entry))
                stack.enter_context(patch.object(local_api_module.os, "close", side_effect=close_descriptor))
                canonical_socket_reader = stack.enter_context(
                    patch.object(
                        local_api_module,
                        "_stat_linux_canonical_local_socket",
                        side_effect=stat_canonical_socket,
                    )
                )
                stack.enter_context(patch.object(local_api_module.socket, "SOL_SOCKET", 1))
                stack.enter_context(
                    patch.object(local_api_module.socket, "SO_PEERCRED", 17, create=True)
                )
                socket_factory = stack.enter_context(
                    patch.object(local_api_module.socket, "socket", side_effect=create_socket)
                )
                stack.enter_context(
                    patch.object(local_api_module.time, "monotonic", side_effect=monotonic)
                )

                observed = read_current_user_local_api_state()

            self.assertEqual(
                observed,
                LinuxLocalAPIState(
                    socket_file_id="71:81",
                    socket_mode=0o600,
                    socket_uid=current_uid,
                    peer_pid=peer_pid,
                    peer_uid=current_uid,
                    peer_gid=current_gid,
                    peer_parent_pid=peer_parent_pid,
                    peer_start_time_ticks=peer_start_time_ticks,
                    peer_executable_file_id="91:92",
                    peer_executable_signature_sha256=hashlib.sha256(
                        struct.pack(
                            ">9Q",
                            peer_executable_fact.st_dev,
                            peer_executable_fact.st_ino,
                            peer_executable_fact.st_mode,
                            peer_executable_fact.st_uid,
                            peer_executable_fact.st_gid,
                            peer_executable_fact.st_nlink,
                            peer_executable_fact.st_size,
                            peer_executable_fact.st_mtime_ns,
                            peer_executable_fact.st_ctime_ns,
                        )
                    ).hexdigest(),
                    peer_executable_path_sha256=hashlib.sha256(
                        os.fsencode(peer_executable)
                    ).hexdigest(),
                    peer_argv_sha256=hashlib.sha256(peer_argv_nul).hexdigest(),
                    peer_cgroup_sha256=hashlib.sha256(
                        peer_cgroup.encode("ascii")
                    ).hexdigest(),
                    http_status=200,
                    schema_version="1.0",
                    health_ok=True,
                    health_status="ok",
                ),
            )
            authority_reader.assert_called_once_with(platform="linux")
            socket_factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
            self.assertEqual(fake_socket.peer_reads, 2)
            self.assertTrue(fake_socket.closed)
            self.assertEqual(home_stats, 2)
            self.assertEqual(held_socket_stats, 1)
            self.assertEqual(canonical_socket_stats, 1)
            canonical_socket_reader.assert_called_once_with(home)
            self.assertTrue(timeouts)
            self.assertLess(observed_times[-1], observed_times[0] + 2.0)
            self.assertEqual(len(closed_descriptors), len(set(closed_descriptors)))
            self.assertEqual(
                set(closed_descriptors),
                set(
                    descriptors
                    + (proc_descriptor, peer_descriptor)
                    + proc_file_descriptors
                ),
            )
            self.assertEqual(closed_descriptors[-6:], [peer_descriptor, proc_descriptor, *reversed(descriptors)])
            self.assertLess(events.index(("held_socket_stat", 1)), events.index(("connect", f"/proc/self/fd/{descriptors[3]}/openusage.sock")))
            self.assertLess(
                events.index(("peer", 1)),
                events.index(("proc_open", "before", "status", proc_file_descriptors[0])),
            )
            self.assertLess(
                events.index(("proc_exe_stat", 1)),
                events.index(("sendall", request)),
            )
            self.assertLess(events.index(("peer", 2)), events.index(("canonical_socket_stat", 1)))
            self.assertLess(events.index(("peer", 1)), events.index(("sendall", request)))
            self.assertLess(events.index(("recv", 0)), events.index(("peer", 2)))
            self.assertLess(
                events.index(("peer", 2)),
                events.index(("proc_open", "after", "status", proc_file_descriptors[4])),
            )
            self.assertLess(
                events.index(("proc_exe_stat", 2)),
                events.index(("canonical_socket_stat", 1)),
            )
            self.assertLess(events.index(("peer", 2)), events.index(("socket_close",)))
            after = tuple(
                (path.relative_to(root).as_posix(), path.lstat().st_mode, path.read_bytes())
                for path in sorted(root.rglob("*"))
                if path.is_file()
            )
            self.assertEqual(after, before)

    def test_read_current_user_local_api_state_rejects_peer_process_drift(self):
        from types import SimpleNamespace

        from openusage_bar.local_api import (
            LocalAPIObservationError,
            _LinuxLocalAPIPeerProcessFact,
            read_current_user_local_api_state,
        )

        executable_signature = (91, 92, stat.S_IFREG | 0o700, os.getuid(), os.getgid(), 1, 1024, 101, 102)
        stable = _LinuxLocalAPIPeerProcessFact(
            uid=os.getuid(),
            gid=os.getgid(),
            parent_pid=4300,
            start_time_ticks=987655,
            executable_path="/tmp/audited/openusage-collector",
            executable_signature=executable_signature,
            argv_nul=b"/tmp/audited/openusage-collector\0daemon\0",
            cgroup=b"0::/user.slice/openusage-bar.service\n",
        )
        cases = (
            ("parent_pid", replace(stable, parent_pid=4301)),
            ("start_time_ticks", replace(stable, start_time_ticks=987656)),
        )
        real_open = local_api_module.os.open
        real_fstat = local_api_module.os.fstat
        real_close = local_api_module.os.close

        for label, drifted in cases:
            with self.subTest(label=label), _LinuxLocalAPIObservationHarness(self) as harness:
                proc_descriptor = 9300
                peer_descriptor = 9301
                synthetic_closed: list[int] = []

                def open_entry(path, flags, *args, dir_fd=None, **kwargs):
                    self.assertEqual(args, ())
                    self.assertEqual(kwargs, {})
                    if path == "/proc":
                        self.assertIsNone(dir_fd)
                        self.assertTrue(flags & os.O_DIRECTORY)
                        self.assertTrue(flags & os.O_NOFOLLOW)
                        return proc_descriptor
                    if (os.fspath(path), dir_fd) == ("4312", proc_descriptor):
                        self.assertTrue(flags & os.O_DIRECTORY)
                        self.assertTrue(flags & os.O_NOFOLLOW)
                        return peer_descriptor
                    return real_open(path, flags, dir_fd=dir_fd)

                def fstat_entry(descriptor):
                    if descriptor == proc_descriptor:
                        return SimpleNamespace(st_mode=stat.S_IFDIR | 0o555, st_uid=0)
                    if descriptor == peer_descriptor:
                        return SimpleNamespace(
                            st_mode=stat.S_IFDIR | 0o555,
                            st_uid=os.getuid(),
                            st_gid=os.getgid(),
                        )
                    return real_fstat(descriptor)

                def close_entry(descriptor):
                    if descriptor in {proc_descriptor, peer_descriptor}:
                        synthetic_closed.append(descriptor)
                        return None
                    return real_close(descriptor)

                with (
                    patch.object(local_api_module.os, "open", side_effect=open_entry),
                    patch.object(local_api_module.os, "fstat", side_effect=fstat_entry),
                    patch.object(local_api_module.os, "close", side_effect=close_entry),
                    patch.object(
                        local_api_module,
                        "_read_linux_local_api_peer_process",
                        side_effect=(stable, drifted),
                    ) as process_reader,
                ):
                    with self.assertRaises(LocalAPIObservationError) as raised:
                        read_current_user_local_api_state()

                self.assertEqual(
                    str(raised.exception),
                    "Local API observation failed",
                )
                process_reader.assert_has_calls(
                    [
                        unittest.mock.call(
                            pid_descriptor=peer_descriptor,
                            pid=4312,
                            current_uid=os.getuid(),
                            current_gid=os.getgid(),
                        ),
                        unittest.mock.call(
                            pid_descriptor=peer_descriptor,
                            pid=4312,
                            current_uid=os.getuid(),
                            current_gid=os.getgid(),
                        ),
                    ]
                )
                self.assertEqual(synthetic_closed, [peer_descriptor, proc_descriptor])
                harness.assert_full_transaction()
                harness.assert_marker_unchanged()

    def test_read_current_user_local_api_state_rejects_final_home_rebind(self):
        import struct

        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from openusage_bar.local_api import (
            LocalAPIObservationError,
            read_current_user_local_api_state,
        )

        real_socket = socket.socket
        servers: list[socket.socket] = []
        with tempfile.TemporaryDirectory(prefix="lua-", dir="/tmp") as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            state_root = home / ".local" / "state" / "openusage-bar"
            state_root.mkdir(parents=True, mode=0o700)
            state_root.chmod(0o700)
            owned_marker = state_root / "owned-marker"
            owned_marker.write_bytes(b"owned-original")
            socket_path = state_root / "openusage.sock"
            original_server = real_socket(socket.AF_UNIX, socket.SOCK_STREAM)
            servers.append(original_server)
            original_server.bind(str(socket_path))
            socket_path.chmod(0o600)
            authority = LifecycleStatePaths(platform="linux", home=home)
            current_uid = os.getuid()
            current_gid = os.getgid()
            peer_pid = 4312
            original_socket_identity = (
                socket_path.lstat().st_dev,
                socket_path.lstat().st_ino,
            )
            original_home = root / "owned-home-original"
            replacement_marker = b"PRIVATE-replacement-tree"
            replacement_socket_identity: list[tuple[int, int]] = []
            rebound = [False]

            payload = json.dumps(
                {
                    "schemaVersion": "1.0",
                    "dataRevision": 7,
                    "generatedAt": "2026-08-12T00:00:00Z",
                    "sources": [],
                    "health": {"ok": True, "status": "ok"},
                },
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + payload
            )
            response_chunks = iter((response[:31], response[31:], b""))
            expected_request = (
                b"GET /v1/health HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Accept: application/json\r\n"
                b"Connection: close\r\n\r\n"
            )
            peer_credentials = struct.pack(
                "=3i",
                peer_pid,
                current_uid,
                current_gid,
            )
            events: list[str] = []

            def rebind_public_home() -> None:
                self.assertFalse(rebound[0])
                home.rename(original_home)
                replacement_root = (
                    home / ".local" / "state" / "openusage-bar"
                )
                replacement_root.mkdir(parents=True, mode=0o700)
                replacement_root.chmod(0o700)
                (replacement_root / "replacement-marker").write_bytes(
                    replacement_marker
                )
                replacement_socket = replacement_root / "openusage.sock"
                replacement_server = real_socket(
                    socket.AF_UNIX,
                    socket.SOCK_STREAM,
                )
                servers.append(replacement_server)
                replacement_server.bind(str(replacement_socket))
                replacement_socket.chmod(0o600)
                replacement_metadata = replacement_socket.lstat()
                replacement_socket_identity.append(
                    (replacement_metadata.st_dev, replacement_metadata.st_ino)
                )
                rebound[0] = True
                events.append("home_rebound")

            class FakeClient:
                def __init__(self):
                    self.closed = False
                    self.peer_reads = 0

                def settimeout(self, timeout):
                    self_outer.assertGreater(timeout, 0.0)
                    self_outer.assertLessEqual(timeout, 1.0)

                def connect(self, address):
                    self_outer.assertTrue(address.startswith("/proc/self/fd/"))
                    self_outer.assertTrue(address.endswith("/openusage.sock"))
                    descriptor = int(address.split("/")[4])
                    held = os.fstat(descriptor)
                    state = state_root.lstat()
                    self_outer.assertEqual(
                        (held.st_dev, held.st_ino),
                        (state.st_dev, state.st_ino),
                    )
                    events.append("connected")

                def getsockopt(self, level, option, size):
                    self_outer.assertEqual((level, option, size), (1, 17, 12))
                    self.peer_reads += 1
                    if self.peer_reads == 2:
                        rebind_public_home()
                    events.append(f"peer_{self.peer_reads}")
                    return peer_credentials

                def sendall(self, request):
                    self_outer.assertEqual(request, expected_request)
                    events.append("request")

                def recv(self, size):
                    self_outer.assertEqual(size, 65_536)
                    chunk = next(response_chunks)
                    events.append(f"recv_{len(chunk)}")
                    return chunk

                def close(self):
                    self_outer.assertFalse(self.closed)
                    self.closed = True
                    events.append("closed")

            self_outer = self
            fake_client = FakeClient()
            clock = [100.0]

            def monotonic():
                value = clock[0]
                clock[0] += 0.05
                return value

            try:
                with patch.object(
                    local_api_module.sys,
                    "platform",
                    "linux",
                ), patch.object(
                    LifecycleStatePaths,
                    "for_current_user",
                    return_value=authority,
                ), patch.object(
                    local_api_module.Path,
                    "home",
                    return_value=home,
                ), patch.object(
                    local_api_module.socket,
                    "socket",
                    return_value=fake_client,
                ), patch.object(
                    local_api_module.time,
                    "monotonic",
                    side_effect=monotonic,
                ), _stable_linux_local_api_peer_process(
                    self,
                ):
                    with self.assertRaisesRegex(
                        LocalAPIObservationError,
                        "Local API observation failed",
                    ) as unavailable:
                        read_current_user_local_api_state()

                self.assertEqual(
                    str(unavailable.exception),
                    "Local API observation failed",
                )
                self.assertNotIn(str(root), str(unavailable.exception))
                self.assertNotIn("PRIVATE", str(unavailable.exception))
                self.assertTrue(rebound[0])
                self.assertEqual(fake_client.peer_reads, 2)
                self.assertTrue(fake_client.closed)
                self.assertIn("recv_0", events)
                self.assertLess(events.index("recv_0"), events.index("home_rebound"))
                moved_socket = (
                    original_home
                    / ".local"
                    / "state"
                    / "openusage-bar"
                    / "openusage.sock"
                )
                self.assertEqual(
                    (moved_socket.lstat().st_dev, moved_socket.lstat().st_ino),
                    original_socket_identity,
                )
                self.assertEqual(
                    (
                        home
                        / ".local"
                        / "state"
                        / "openusage-bar"
                        / "openusage.sock"
                    ).lstat().st_ino,
                    replacement_socket_identity[0][1],
                )
                self.assertEqual(
                    (
                        original_home
                        / ".local"
                        / "state"
                        / "openusage-bar"
                        / "owned-marker"
                    ).read_bytes(),
                    b"owned-original",
                )
                self.assertEqual(
                    (
                        home
                        / ".local"
                        / "state"
                        / "openusage-bar"
                        / "replacement-marker"
                    ).read_bytes(),
                    replacement_marker,
                )
            finally:
                for server in servers:
                    server.close()

    def test_read_current_user_local_api_state_rechecks_socket_after_directory_bindings(self):
        import struct

        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from openusage_bar.local_api import (
            LocalAPIObservationError,
            read_current_user_local_api_state,
        )

        real_socket = socket.socket
        real_stat = os.stat
        servers: list[socket.socket] = []
        with tempfile.TemporaryDirectory(prefix="lua-", dir="/tmp") as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            state_root = home / ".local" / "state" / "openusage-bar"
            state_root.mkdir(parents=True, mode=0o700)
            state_root.chmod(0o700)
            marker = state_root / "owned-marker"
            marker.write_bytes(b"owned-state-root")
            socket_path = state_root / "openusage.sock"
            owned_server = real_socket(socket.AF_UNIX, socket.SOCK_STREAM)
            servers.append(owned_server)
            owned_server.bind(str(socket_path))
            socket_path.chmod(0o600)
            owned_identity = (socket_path.lstat().st_dev, socket_path.lstat().st_ino)
            owned_original = state_root / "owned-socket-original"
            authority = LifecycleStatePaths(platform="linux", home=home)
            current_uid = os.getuid()
            peer_credentials = struct.pack(
                "=3i",
                4312,
                current_uid,
                os.getgid(),
            )
            swapped = [False]
            binding_reads = [0]
            foreign_identity: list[tuple[int, int]] = []
            events: list[str] = []

            def swap_socket_name() -> None:
                socket_path.rename(owned_original)
                foreign_server = real_socket(socket.AF_UNIX, socket.SOCK_STREAM)
                servers.append(foreign_server)
                foreign_server.bind(str(socket_path))
                socket_path.chmod(0o600)
                metadata = socket_path.lstat()
                foreign_identity.append((metadata.st_dev, metadata.st_ino))
                swapped[0] = True
                events.append("socket_swapped")

            def stat_with_swap(
                path,
                *args,
                dir_fd=None,
                follow_symlinks=True,
                **kwargs,
            ):
                if (
                    os.fspath(path) == "openusage-bar"
                    and dir_fd is not None
                    and follow_symlinks is False
                ):
                    binding_reads[0] += 1
                    if binding_reads[0] == 2:
                        swap_socket_name()
                return real_stat(
                    path,
                    *args,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                    **kwargs,
                )

            payload = json.dumps(
                {
                    "schemaVersion": "1.0",
                    "dataRevision": 7,
                    "generatedAt": "2026-08-12T00:00:00Z",
                    "sources": [],
                    "health": {"ok": True, "status": "ok"},
                },
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + payload
            )
            chunks = iter((response, b""))

            class FakeClient:
                def __init__(self):
                    self.closed = False
                    self.peer_reads = 0

                def settimeout(self, timeout):
                    self_outer.assertGreater(timeout, 0.0)

                def connect(self, address):
                    self_outer.assertRegex(
                        address,
                        r"^/proc/self/fd/[0-9]+/openusage\.sock$",
                    )

                def getsockopt(self, level, option, size):
                    self_outer.assertEqual((level, option, size), (1, 17, 12))
                    self.peer_reads += 1
                    events.append(f"peer_{self.peer_reads}")
                    return peer_credentials

                def sendall(self, request):
                    self_outer.assertEqual(
                        request,
                        b"GET /v1/health HTTP/1.1\r\n"
                        b"Host: localhost\r\n"
                        b"Accept: application/json\r\n"
                        b"Connection: close\r\n\r\n",
                    )

                def recv(self, size):
                    self_outer.assertEqual(size, 65_536)
                    chunk = next(chunks)
                    events.append(f"recv_{len(chunk)}")
                    return chunk

                def close(self):
                    self_outer.assertFalse(self.closed)
                    self.closed = True
                    events.append("closed")

            self_outer = self
            client = FakeClient()
            clock = [100.0]

            def monotonic():
                value = clock[0]
                clock[0] += 0.05
                return value

            try:
                with patch.object(
                    local_api_module.sys,
                    "platform",
                    "linux",
                ), patch.object(
                    LifecycleStatePaths,
                    "for_current_user",
                    return_value=authority,
                ), patch.object(
                    local_api_module.Path,
                    "home",
                    return_value=home,
                ), patch.object(
                    local_api_module.os,
                    "stat",
                    side_effect=stat_with_swap,
                ), patch.object(
                    local_api_module.socket,
                    "socket",
                    return_value=client,
                ), patch.object(
                    local_api_module.time,
                    "monotonic",
                    side_effect=monotonic,
                ), _stable_linux_local_api_peer_process(
                    self,
                ):
                    with self.assertRaisesRegex(
                        LocalAPIObservationError,
                        "Local API observation failed",
                    ) as unavailable:
                        read_current_user_local_api_state()

                self.assertEqual(
                    str(unavailable.exception),
                    "Local API observation failed",
                )
                self.assertNotIn(str(root), str(unavailable.exception))
                self.assertTrue(swapped[0])
                self.assertEqual(binding_reads[0], 2)
                self.assertEqual(client.peer_reads, 2)
                self.assertTrue(client.closed)
                self.assertIn("recv_0", events)
                self.assertLess(
                    events.index("recv_0"),
                    events.index("socket_swapped"),
                )
                self.assertEqual(
                    (owned_original.lstat().st_dev, owned_original.lstat().st_ino),
                    owned_identity,
                )
                self.assertEqual(
                    (socket_path.lstat().st_dev, socket_path.lstat().st_ino),
                    foreign_identity[0],
                )
                self.assertEqual(marker.read_bytes(), b"owned-state-root")
            finally:
                for server in servers:
                    server.close()

    def test_read_current_user_local_api_state_rejects_state_root_rebind_after_final_binding(self):
        import struct

        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from openusage_bar.local_api import (
            LocalAPIObservationError,
            read_current_user_local_api_state,
        )

        real_socket = socket.socket
        real_stat = os.stat
        servers: list[socket.socket] = []
        with tempfile.TemporaryDirectory(prefix="lua-", dir="/tmp") as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            state_parent = home / ".local" / "state"
            state_root = state_parent / "openusage-bar"
            state_root.mkdir(parents=True, mode=0o700)
            state_root.chmod(0o700)
            owned_marker = state_root / "owned-marker"
            owned_marker.write_bytes(b"owned-original")
            socket_path = state_root / "openusage.sock"
            owned_server = real_socket(socket.AF_UNIX, socket.SOCK_STREAM)
            servers.append(owned_server)
            owned_server.bind(str(socket_path))
            socket_path.chmod(0o600)
            owned_socket_identity = (
                socket_path.lstat().st_dev,
                socket_path.lstat().st_ino,
            )
            authority = LifecycleStatePaths(platform="linux", home=home)
            original_state_root = state_parent / "owned-openusage-original"
            foreign_marker = b"PRIVATE-foreign-state-root"
            foreign_socket_identity: list[tuple[int, int]] = []
            binding_reads = [0]
            rebound = [False]

            def rebind_after_observing_final_binding():
                state_root.rename(original_state_root)
                state_root.mkdir(mode=0o700)
                state_root.chmod(0o700)
                (state_root / "foreign-marker").write_bytes(foreign_marker)
                replacement_socket = state_root / "openusage.sock"
                foreign_server = real_socket(socket.AF_UNIX, socket.SOCK_STREAM)
                servers.append(foreign_server)
                foreign_server.bind(str(replacement_socket))
                replacement_socket.chmod(0o600)
                metadata = replacement_socket.lstat()
                foreign_socket_identity.append((metadata.st_dev, metadata.st_ino))
                rebound[0] = True

            def stat_with_rebind(
                path,
                *args,
                dir_fd=None,
                follow_symlinks=True,
                **kwargs,
            ):
                metadata = real_stat(
                    path,
                    *args,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                    **kwargs,
                )
                if (
                    os.fspath(path) == "openusage-bar"
                    and dir_fd is not None
                    and follow_symlinks is False
                ):
                    binding_reads[0] += 1
                    if binding_reads[0] == 2:
                        rebind_after_observing_final_binding()
                return metadata

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
            client = _LinuxObservationClient(
                response,
                struct.pack("=3i", 4312, os.getuid(), os.getgid()),
            )
            clock = [100.0]

            def monotonic():
                value = clock[0]
                clock[0] += 0.05
                return value

            try:
                with patch.object(
                    local_api_module.sys,
                    "platform",
                    "linux",
                ), patch.object(
                    LifecycleStatePaths,
                    "for_current_user",
                    return_value=authority,
                ), patch.object(
                    local_api_module.Path,
                    "home",
                    return_value=home,
                ), patch.object(
                    local_api_module.os,
                    "stat",
                    side_effect=stat_with_rebind,
                ), patch.object(
                    local_api_module.socket,
                    "socket",
                    return_value=client,
                ), patch.object(
                    local_api_module.time,
                    "monotonic",
                    side_effect=monotonic,
                ), _stable_linux_local_api_peer_process(
                    self,
                ):
                    with self.assertRaisesRegex(
                        LocalAPIObservationError,
                        "Local API observation failed",
                    ) as unavailable:
                        read_current_user_local_api_state()

                self.assertEqual(
                    str(unavailable.exception),
                    "Local API observation failed",
                )
                self.assertNotIn(str(root), str(unavailable.exception))
                self.assertNotIn("PRIVATE", str(unavailable.exception))
                self.assertTrue(rebound[0])
                self.assertEqual(binding_reads[0], 2)
                self.assertEqual(
                    (
                        client.connected,
                        client.peer_reads,
                        client.request_count,
                        client.eof_reads,
                    ),
                    (1, 2, 1, 1),
                )
                self.assertTrue(client.closed)
                moved_socket = original_state_root / "openusage.sock"
                self.assertEqual(
                    (moved_socket.lstat().st_dev, moved_socket.lstat().st_ino),
                    owned_socket_identity,
                )
                self.assertEqual(
                    (socket_path.lstat().st_dev, socket_path.lstat().st_ino),
                    foreign_socket_identity[0],
                )
                self.assertEqual(
                    (original_state_root / "owned-marker").read_bytes(),
                    b"owned-original",
                )
                self.assertEqual(
                    (state_root / "foreign-marker").read_bytes(),
                    foreign_marker,
                )
            finally:
                for server in servers:
                    server.close()

    def test_read_current_user_local_api_state_rejects_symlinked_state_root_after_final_binding(self):
        from openusage_bar.local_api import (
            LocalAPIObservationError,
            read_current_user_local_api_state,
        )

        harness = _LinuxLocalAPIObservationHarness(
            self,
            marker=b"owned-original",
        )
        real_stat = os.stat
        original_state_root = harness.state_parent / "owned-openusage-original"
        binding_reads = [0]
        rebound = [False]

        def stat_with_symlink_rebind(
            path,
            *args,
            dir_fd=None,
            follow_symlinks=True,
            **kwargs,
        ):
            metadata = real_stat(
                path,
                *args,
                dir_fd=dir_fd,
                follow_symlinks=follow_symlinks,
                **kwargs,
            )
            if (
                os.fspath(path) == "openusage-bar"
                and dir_fd is not None
                and follow_symlinks is False
            ):
                binding_reads[0] += 1
                if binding_reads[0] == 2:
                    harness.state_root.rename(original_state_root)
                    harness.state_root.symlink_to(
                        original_state_root.name,
                        target_is_directory=True,
                    )
                    rebound[0] = True
            return metadata

        harness.stat_side_effect = stat_with_symlink_rebind
        with harness:
            with self.assertRaisesRegex(
                LocalAPIObservationError,
                "Local API observation failed",
            ) as unavailable:
                read_current_user_local_api_state()
            self.assertEqual(
                str(unavailable.exception),
                "Local API observation failed",
            )
            self.assertNotIn(str(harness.root), str(unavailable.exception))
            self.assertNotIn("PRIVATE", str(unavailable.exception))
            self.assertTrue(rebound[0])
            self.assertEqual(binding_reads[0], 2)
            harness.assert_full_transaction()
            self.assertTrue(harness.state_root.is_symlink())
            self.assertEqual(
                os.readlink(harness.state_root),
                original_state_root.name,
            )
            self.assertEqual(
                harness.identity(original_state_root / "openusage.sock"),
                harness.socket_identity,
            )
            self.assertEqual(
                (original_state_root / "observation-marker").read_bytes(),
                b"owned-original",
            )

    def test_read_current_user_local_api_state_requires_complete_health_envelope(self):
        from openusage_bar.local_api import (
            LocalAPIObservationError,
            read_current_user_local_api_state,
        )

        for missing_key in ("generatedAt", "sources"):
            with self.subTest(missing=missing_key):
                payload = dict(_COMPLETE_HEALTH_PAYLOAD)
                del payload[missing_key]
                with _LinuxLocalAPIObservationHarness(
                    self,
                    _health_response(payload),
                ) as harness:
                    with self.assertRaisesRegex(
                        LocalAPIObservationError,
                        "Local API observation failed",
                    ) as unavailable:
                        read_current_user_local_api_state()
                    self.assertEqual(
                        str(unavailable.exception),
                        "Local API observation failed",
                    )
                    self.assertNotIn(str(harness.root), str(unavailable.exception))
                    self.assertNotIn("PRIVATE", str(unavailable.exception))
                    harness.assert_full_transaction()
                    harness.assert_marker_unchanged()

    def test_read_current_user_local_api_state_accepts_real_health_router_envelope(self):
        from openusage_bar.local_api import (
            LinuxLocalAPIState,
            read_current_user_local_api_state,
        )

        with tempfile.TemporaryDirectory(prefix="lua-", dir="/tmp") as directory:
            root = Path(directory)
            router_socket = root / "router" / "api.sock"
            store, query = seeded_query()
            router = create_unix_server(router_socket, query, clock=lambda: NOW)
            router_thread = start(router)
            try:
                raw_response = raw_exchange(
                    router_socket,
                    b"GET /v1/health HTTP/1.1\r\n"
                    b"Host: localhost\r\n"
                    b"Accept: application/json\r\n"
                    b"Connection: close\r\n\r\n",
                )
            finally:
                router.shutdown()
                router.server_close()
                router_thread.join(2)
                store.close()
            status_line, _headers, body = split_raw_response(raw_response)
            self.assertEqual(status_line, b"HTTP/1.1 200 OK")
            real_payload = json.loads(body)
            self.assertEqual(
                set(real_payload),
                {
                    "schemaVersion",
                    "dataRevision",
                    "generatedAt",
                    "sources",
                    "health",
                },
            )
        with _LinuxLocalAPIObservationHarness(self, raw_response) as harness:
            observed = read_current_user_local_api_state()
            self.assertEqual(
                observed,
                LinuxLocalAPIState(
                    socket_file_id=(
                        f"{harness.socket_identity[0]}:{harness.socket_identity[1]}"
                    ),
                    socket_mode=0o600,
                    socket_uid=os.getuid(),
                    peer_pid=4312,
                    peer_uid=os.getuid(),
                    peer_gid=os.getgid(),
                    peer_parent_pid=harness.peer_process.process.parent_pid,
                    peer_start_time_ticks=(
                        harness.peer_process.process.start_time_ticks
                    ),
                    peer_executable_file_id="91:92",
                    peer_executable_signature_sha256=hashlib.sha256(
                        struct.pack(
                            ">9Q",
                            *harness.peer_process.process.executable_signature,
                        )
                    ).hexdigest(),
                    peer_executable_path_sha256=hashlib.sha256(
                        os.fsencode(
                            harness.peer_process.process.executable_path
                        )
                    ).hexdigest(),
                    peer_argv_sha256=hashlib.sha256(
                        harness.peer_process.process.argv_nul
                    ).hexdigest(),
                    peer_cgroup_sha256=hashlib.sha256(
                        harness.peer_process.process.cgroup
                    ).hexdigest(),
                    http_status=200,
                    schema_version="1.0",
                    health_ok=True,
                    health_status="ok",
                ),
            )
            harness.socket_factory.assert_called_once_with(
                socket.AF_UNIX,
                socket.SOCK_STREAM,
            )
            harness.assert_full_transaction()
            harness.assert_marker_unchanged()

    def test_read_current_user_local_api_state_rejects_unsafe_authority_ancestors(self):
        import struct

        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from openusage_bar.local_api import (
            LocalAPIObservationError,
            read_current_user_local_api_state,
        )

        payload = json.dumps(
            {
                "schemaVersion": "1.0",
                "dataRevision": 7,
                "generatedAt": "2026-08-12T00:00:00Z",
                "sources": [],
                "health": {"ok": True, "status": "ok"},
            },
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(payload)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + payload
        )
        for component, unsafe_mode in (
            ("home", 0o770),
            (".local", 0o707),
            ("state", 0o777),
        ):
            with self.subTest(component=component), tempfile.TemporaryDirectory(
                prefix="lua-",
                dir="/tmp",
            ) as directory:
                root = Path(directory)
                home = root / "authoritative-home"
                local_root = home / ".local"
                state_parent = local_root / "state"
                state_root = state_parent / "openusage-bar"
                state_root.mkdir(parents=True, mode=0o700)
                state_root.chmod(0o700)
                unsafe_path = {
                    "home": home,
                    ".local": local_root,
                    "state": state_parent,
                }[component]
                unsafe_path.chmod(unsafe_mode)
                marker = state_root / "PRIVATE-authority-marker"
                marker.write_bytes(b"unchanged")
                socket_path = state_root / "openusage.sock"
                bound_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                bound_socket.bind(str(socket_path))
                socket_path.chmod(0o600)
                authority = LifecycleStatePaths(platform="linux", home=home)
                client = _LinuxObservationClient(
                    response,
                    struct.pack("=3i", 4312, os.getuid(), os.getgid()),
                )
                clock = [100.0]

                def monotonic():
                    value = clock[0]
                    clock[0] += 0.05
                    return value

                try:
                    with patch.object(
                        local_api_module.sys,
                        "platform",
                        "linux",
                    ), patch.object(
                        LifecycleStatePaths,
                        "for_current_user",
                        return_value=authority,
                    ), patch.object(
                        local_api_module.Path,
                        "home",
                        return_value=home,
                    ), patch.object(
                        local_api_module.socket,
                        "socket",
                        return_value=client,
                    ) as socket_factory, patch.object(
                        local_api_module.time,
                        "monotonic",
                        side_effect=monotonic,
                    ):
                        with self.assertRaisesRegex(
                            LocalAPIObservationError,
                            "Local API observation failed",
                        ) as unavailable:
                            read_current_user_local_api_state()

                    self.assertEqual(
                        str(unavailable.exception),
                        "Local API observation failed",
                    )
                    self.assertNotIn(str(root), str(unavailable.exception))
                    self.assertNotIn("PRIVATE", str(unavailable.exception))
                    socket_factory.assert_not_called()
                    self.assertEqual(
                        (
                            client.connected,
                            client.peer_reads,
                            client.request_count,
                        ),
                        (0, 0, 0),
                    )
                    self.assertEqual(marker.read_bytes(), b"unchanged")
                finally:
                    bound_socket.close()

    def test_read_current_user_local_api_state_accepts_only_a_private_primary_group_local_root(self):
        from openusage_bar.local_api import _linux_local_api_ancestor_is_private

        current_uid = os.getuid()
        current_gid = os.getgid()
        metadata = SimpleNamespace(
            st_uid=current_uid,
            st_gid=current_gid,
            st_mode=stat.S_IFDIR | 0o775,
        )
        with patch(
            "openusage_bar.local_api.pwd.getpwuid",
            return_value=SimpleNamespace(pw_name="usagehub", pw_gid=current_gid),
        ), patch(
            "openusage_bar.local_api.grp.getgrgid",
            return_value=SimpleNamespace(gr_name="usagehub", gr_mem=[]),
        ):
            self.assertTrue(
                _linux_local_api_ancestor_is_private(
                    metadata,
                    current_uid=current_uid,
                    current_gid=current_gid,
                )
            )
        for group_name, members, mode in (
            ("shared", [], 0o775),
            ("usagehub", ["foreign"], 0o775),
            ("usagehub", [], 0o777),
        ):
            with self.subTest(group_name=group_name, members=members, mode=mode):
                metadata.st_mode = stat.S_IFDIR | mode
                with patch(
                    "openusage_bar.local_api.pwd.getpwuid",
                    return_value=SimpleNamespace(
                        pw_name="usagehub", pw_gid=current_gid
                    ),
                ), patch(
                    "openusage_bar.local_api.grp.getgrgid",
                    return_value=SimpleNamespace(
                        gr_name=group_name,
                        gr_mem=members,
                    ),
                ):
                    self.assertFalse(
                        _linux_local_api_ancestor_is_private(
                            metadata,
                            current_uid=current_uid,
                            current_gid=current_gid,
                        )
                    )

    def test_read_current_user_local_api_state_fails_closed_on_http_json_and_clock_uncertainty(self):
        import struct

        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from openusage_bar.local_api import (
            LocalAPIObservationError,
            read_current_user_local_api_state,
        )

        valid_payload = (
            b'{"schemaVersion":"1.0","dataRevision":7,'
            b'"generatedAt":"2026-08-12T00:00:00Z","sources":[],'
            b'"health":{"ok":true,"status":"ok"}}'
        )

        def response(
            body=valid_payload,
            *,
            status=b"HTTP/1.1 200 OK",
            headers=(),
            content_length=None,
        ):
            length = len(body) if content_length is None else content_length
            return (
                status
                + b"\r\nContent-Type: application/json; charset=utf-8\r\n"
                + f"Content-Length: {length}\r\n".encode("ascii")
                + b"Connection: close\r\n"
                + b"".join(header + b"\r\n" for header in headers)
                + b"\r\n"
                + body
            )

        duplicate_json = (
            b'{"schemaVersion":"1.0","dataRevision":7,'
            b'"generatedAt":"2026-08-12T00:00:00Z","sources":[],'
            b'"sources":[],"health":{"ok":true,"status":"ok"}}'
        )
        nan_json = valid_payload.replace(b'"dataRevision":7', b'"dataRevision":NaN')
        non_utc_json = valid_payload.replace(
            b"2026-08-12T00:00:00Z",
            b"not-a-utc-timestamp",
        )
        wrong_sources_json = valid_payload.replace(b'"sources":[]', b'"sources":{}')
        wrong_source_item_json = valid_payload.replace(
            b'"sources":[]',
            b'"sources":[{"PRIVATE_key":"PRIVATE_value"}]',
        )
        cases = (
            ("status", response(status=b"HTTP/1.1 503 Service Unavailable"), None, None, 2, 1, 1),
            ("duplicate_content_length", response(headers=(b"Content-Length: 1",)), None, None, 2, 1, 1),
            ("transfer_encoding", response(headers=(b"Transfer-Encoding: chunked",)), None, None, 2, 1, 1),
            ("length_mismatch", response(content_length=len(valid_payload) + 1), None, None, 2, 1, 1),
            ("body_overflow", response(b"x" * 65_537), None, None, 2, 1, 1),
            ("duplicate_json_key", response(duplicate_json), None, None, 2, 1, 1),
            ("nan_json", response(nan_json), None, None, 2, 1, 1),
            ("generated_at_not_utc", response(non_utc_json), None, None, 2, 1, 1),
            ("sources_wrong_type", response(wrong_sources_json), None, None, 2, 1, 1),
            ("sources_item_wrong_keys", response(wrong_source_item_json), None, None, 2, 1, 1),
            ("clock_regression", response(), None, (100.0, 100.1, 100.05), 1, 0, 0),
            ("clock_expired", response(), None, (100.0, 102.0), 0, 0, 0),
            ("recv_non_bytes", response(), bytearray(b"not-bytes"), None, 1, 1, 0),
            ("recv_timeout", response(), TimeoutError("PRIVATE_TIMEOUT"), None, 1, 1, 0),
        )
        for (
            case_name,
            raw_response,
            recv_override,
            clock_values,
            expected_peer_reads,
            expected_requests,
            expected_eof_reads,
        ) in cases:
            with self.subTest(case=case_name), tempfile.TemporaryDirectory(
                prefix="lua-",
                dir="/tmp",
            ) as directory:
                root = Path(directory)
                home = root / "authoritative-home"
                state_root = home / ".local" / "state" / "openusage-bar"
                state_root.mkdir(parents=True, mode=0o700)
                state_root.chmod(0o700)
                marker = state_root / "PRIVATE-matrix-marker"
                marker.write_bytes(b"unchanged")
                socket_path = state_root / "openusage.sock"
                bound_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                bound_socket.bind(str(socket_path))
                socket_path.chmod(0o600)
                authority = LifecycleStatePaths(platform="linux", home=home)
                peer = struct.pack("=3i", 4312, os.getuid(), os.getgid())

                class CaseClient:
                    def __init__(self):
                        self.chunks = iter(
                            (raw_response[:31], raw_response[31:], b"")
                        )
                        self.connected = 0
                        self.peer_reads = 0
                        self.request_count = 0
                        self.eof_reads = 0
                        self.closed = False

                    def settimeout(self, timeout):
                        test_case.assertGreater(timeout, 0.0)

                    def connect(self, address):
                        test_case.assertRegex(
                            address,
                            r"^/proc/self/fd/[0-9]+/openusage\.sock$",
                        )
                        self.connected += 1

                    def getsockopt(self, level, option, size):
                        test_case.assertEqual((level, option, size), (1, 17, 12))
                        self.peer_reads += 1
                        return peer

                    def sendall(self, request):
                        test_case.assertIn(b"GET /v1/health HTTP/1.1", request)
                        self.request_count += 1

                    def recv(self, size):
                        test_case.assertEqual(size, 65_536)
                        if recv_override is not None:
                            if isinstance(recv_override, BaseException):
                                raise recv_override
                            return recv_override
                        chunk = next(self.chunks)
                        if not chunk:
                            self.eof_reads += 1
                        return chunk

                    def close(self):
                        test_case.assertFalse(self.closed)
                        self.closed = True

                test_case = self
                client = CaseClient()
                if clock_values is None:
                    next_time = [100.0]

                    def monotonic():
                        value = next_time[0]
                        next_time[0] += 0.05
                        return value

                else:
                    clock_iterator = iter(clock_values)

                    def monotonic():
                        return next(clock_iterator)

                try:
                    with patch.object(
                        local_api_module.sys,
                        "platform",
                        "linux",
                    ), patch.object(
                        LifecycleStatePaths,
                        "for_current_user",
                        return_value=authority,
                    ), patch.object(
                        local_api_module.Path,
                        "home",
                        return_value=home,
                    ), patch.object(
                        local_api_module.socket,
                        "socket",
                        return_value=client,
                    ), patch.object(
                        local_api_module.time,
                        "monotonic",
                        side_effect=monotonic,
                    ), _stable_linux_local_api_peer_process(
                        self,
                    ):
                        with self.assertRaisesRegex(
                            LocalAPIObservationError,
                            "Local API observation failed",
                        ) as unavailable:
                            read_current_user_local_api_state()

                    self.assertEqual(
                        str(unavailable.exception),
                        "Local API observation failed",
                    )
                    self.assertNotIn(str(root), str(unavailable.exception))
                    self.assertNotIn("PRIVATE", str(unavailable.exception))
                    self.assertEqual(client.peer_reads, expected_peer_reads)
                    self.assertEqual(client.request_count, expected_requests)
                    self.assertEqual(client.eof_reads, expected_eof_reads)
                    self.assertTrue(client.closed)
                    self.assertEqual(marker.read_bytes(), b"unchanged")
                finally:
                    bound_socket.close()

    def test_read_current_user_local_api_state_fails_closed_on_identity_flags_and_cleanup(self):
        import struct
        from contextlib import ExitStack
        from types import SimpleNamespace

        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from openusage_bar.local_api import (
            LocalAPIObservationError,
            read_current_user_local_api_state,
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
        valid_peer = struct.pack("=3i", 4312, os.getuid(), os.getgid())

        def exercise(
            case_name,
            *,
            socket_overrides=None,
            peer_values=None,
            client_close_failure=False,
            directory_close_failure=False,
            missing_flag=None,
            expected_peer_reads=0,
        ):
            with self.subTest(case=case_name), tempfile.TemporaryDirectory(
                prefix="lua-",
                dir="/tmp",
            ) as directory:
                root = Path(directory)
                home = root / "authoritative-home"
                state_root = home / ".local" / "state" / "openusage-bar"
                state_root.mkdir(parents=True, mode=0o700)
                state_root.chmod(0o700)
                marker = state_root / "PRIVATE-boundary-marker"
                marker.write_bytes(b"unchanged")
                socket_path = state_root / "openusage.sock"
                bound_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                bound_socket.bind(str(socket_path))
                socket_path.chmod(0o600)
                authority = LifecycleStatePaths(platform="linux", home=home)
                real_stat = os.stat
                real_open = os.open
                real_close = os.close
                canonical_socket_fact = real_stat(
                    socket_path,
                    follow_symlinks=False,
                )
                opened_descriptors: list[int] = []
                close_attempts: list[int] = []
                socket_stat_reads = [0]

                def stat_router(
                    path,
                    *args,
                    dir_fd=None,
                    follow_symlinks=True,
                    **kwargs,
                ):
                    metadata = real_stat(
                        path,
                        *args,
                        dir_fd=dir_fd,
                        follow_symlinks=follow_symlinks,
                        **kwargs,
                    )
                    if os.fspath(path) != "openusage.sock" or dir_fd is None:
                        return metadata
                    socket_stat_reads[0] += 1
                    if socket_stat_reads[0] != 1 or socket_overrides is None:
                        return metadata
                    values = {
                        "st_mode": metadata.st_mode,
                        "st_uid": metadata.st_uid,
                        "st_gid": metadata.st_gid,
                        "st_nlink": metadata.st_nlink,
                        "st_dev": metadata.st_dev,
                        "st_ino": metadata.st_ino,
                        "st_size": metadata.st_size,
                        "st_mtime_ns": metadata.st_mtime_ns,
                        "st_ctime_ns": metadata.st_ctime_ns,
                    }
                    values.update(socket_overrides)
                    return SimpleNamespace(**values)

                def open_router(path, flags, *args, **kwargs):
                    descriptor = real_open(path, flags, *args, **kwargs)
                    opened_descriptors.append(descriptor)
                    return descriptor

                def close_router(descriptor):
                    close_attempts.append(descriptor)
                    real_close(descriptor)
                    if directory_close_failure and len(close_attempts) == 1:
                        raise RuntimeError("PRIVATE_DIRECTORY_CLOSE")

                def stat_canonical_socket(selected_home):
                    self.assertEqual(selected_home, home)
                    return canonical_socket_fact

                class BoundaryClient(_LinuxObservationClient):
                    def __init__(self):
                        super().__init__(response, valid_peer)
                        self.peer_iterator = iter(
                            (valid_peer, valid_peer)
                            if peer_values is None
                            else peer_values
                        )

                    def getsockopt(self, level, option, size):
                        assert (level, option, size) == (1, 17, 12)
                        self.peer_reads += 1
                        return next(self.peer_iterator)

                    def close(self):
                        assert not self.closed
                        self.closed = True
                        if client_close_failure:
                            raise RuntimeError("PRIVATE_SOCKET_CLOSE")

                client = BoundaryClient()
                clock = [100.0]

                def monotonic():
                    value = clock[0]
                    clock[0] += 0.05
                    return value

                try:
                    with ExitStack() as stack:
                        stack.enter_context(
                            patch.object(local_api_module.sys, "platform", "linux")
                        )
                        stack.enter_context(
                            patch.object(
                                LifecycleStatePaths,
                                "for_current_user",
                                return_value=authority,
                            )
                        )
                        stack.enter_context(
                            patch.object(
                                local_api_module.Path,
                                "home",
                                return_value=home,
                            )
                        )
                        stack.enter_context(
                            patch.object(
                                local_api_module.os,
                                "stat",
                                side_effect=stat_router,
                            )
                        )
                        canonical_socket_reader = stack.enter_context(
                            patch.object(
                                local_api_module,
                                "_stat_linux_canonical_local_socket",
                                side_effect=stat_canonical_socket,
                            )
                        )
                        socket_factory = stack.enter_context(
                            patch.object(
                                local_api_module.socket,
                                "socket",
                                return_value=client,
                            )
                        )
                        stack.enter_context(
                            patch.object(
                                local_api_module.time,
                                "monotonic",
                                side_effect=monotonic,
                            )
                        )
                        if directory_close_failure:
                            stack.enter_context(
                                patch.object(
                                    local_api_module.os,
                                    "open",
                                    side_effect=open_router,
                                )
                            )
                            stack.enter_context(
                                patch.object(
                                    local_api_module.os,
                                    "close",
                                    side_effect=close_router,
                                )
                            )
                        if missing_flag is not None:
                            stack.enter_context(
                                patch.object(local_api_module.os, missing_flag, 0)
                            )
                        stack.enter_context(
                            _stable_linux_local_api_peer_process(self)
                        )

                        with self.assertRaisesRegex(
                            LocalAPIObservationError,
                            "Local API observation failed",
                        ) as unavailable:
                            read_current_user_local_api_state()

                    self.assertEqual(
                        str(unavailable.exception),
                        "Local API observation failed",
                    )
                    self.assertNotIn(str(root), str(unavailable.exception))
                    self.assertNotIn("PRIVATE", str(unavailable.exception))
                    self.assertEqual(client.peer_reads, expected_peer_reads)
                    self.assertEqual(marker.read_bytes(), b"unchanged")
                    if missing_flag is not None or socket_overrides is not None:
                        socket_factory.assert_not_called()
                    else:
                        socket_factory.assert_called_once_with(
                            socket.AF_UNIX,
                            socket.SOCK_STREAM,
                        )
                        self.assertTrue(client.closed)
                    if directory_close_failure:
                        self.assertEqual(len(opened_descriptors), 4)
                        self.assertEqual(
                            close_attempts,
                            list(reversed(opened_descriptors)),
                        )
                    self.assertEqual(
                        canonical_socket_reader.call_count,
                        int(client_close_failure or directory_close_failure),
                    )
                finally:
                    bound_socket.close()

        for case_name, overrides in (
            ("socket_type", {"st_mode": stat.S_IFREG | 0o600}),
            ("socket_uid", {"st_uid": os.getuid() + 1}),
            ("socket_mode", {"st_mode": stat.S_IFSOCK | 0o640}),
            ("socket_nlink", {"st_nlink": 2}),
        ):
            exercise(
                case_name,
                socket_overrides=overrides,
                expected_peer_reads=0,
            )
        for case_name, peers, expected_reads in (
            ("peer_raw_type", (bytearray(valid_peer),), 1),
            ("peer_raw_length", (valid_peer[:8],), 1),
            ("peer_pid", (struct.pack("=3i", 0, os.getuid(), os.getgid()),), 1),
            ("peer_uid", (struct.pack("=3i", 4312, os.getuid() + 1, os.getgid()),), 1),
            ("peer_gid", (struct.pack("=3i", 4312, os.getuid(), os.getgid() + 1),), 1),
            ("peer_drift", (valid_peer, struct.pack("=3i", 4313, os.getuid(), os.getgid())), 2),
        ):
            exercise(
                case_name,
                peer_values=peers,
                expected_peer_reads=expected_reads,
            )
        exercise(
            "socket_close_failure",
            client_close_failure=True,
            expected_peer_reads=2,
        )
        exercise(
            "directory_close_failure",
            directory_close_failure=True,
            expected_peer_reads=2,
        )
        exercise("missing_nofollow", missing_flag="O_NOFOLLOW")
        exercise("missing_directory", missing_flag="O_DIRECTORY")


@unittest.skipIf(sys.platform == "win32", "Windows uses loopback TCP transport")
class UnixLocalAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "private"
        self.socket_path = self.root / "api.sock"
        self.store, self.query = seeded_query()
        self.server = create_unix_server(
            self.socket_path,
            self.query,
            clock=lambda: NOW,
            observer_platform=ObserverPlatformResolver(
                catalog, runtime_platform="darwin"
            ),
        )
        self.thread = start(self.server)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.store.close()
        self.temp.cleanup()

    def request(self, target, **kwargs):
        return unix_request(self.socket_path, target, **kwargs)

    def test_default_unix_socket_and_parent_are_user_only_and_cleaned(self):
        self.assertTrue(stat.S_ISSOCK(self.socket_path.stat().st_mode))
        self.assertEqual(stat.S_IMODE(self.socket_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.socket_path.exists())

    def test_unix_transport_alone_exposes_closed_shared_client_boundary_snapshot(self):
        from openusage_bar.shared_client_boundary import (
            shared_client_boundary_attempt_counters,
        )

        expected = shared_client_boundary_attempt_counters()
        target = "/_internal/v1/shared-client-boundary-attempts"
        status, headers, body = self.request(target)
        self.assertEqual(status, 200)
        self.assertEqual(
            headers,
            {
                "server": "OpenUsageLocalAPI/1 ",
                "date": headers["date"],
                "content-type": "application/json; charset=utf-8",
                "cache-control": "no-store",
                "x-content-type-options": "nosniff",
                "connection": "close",
                "content-length": str(len(body)),
            },
        )
        self.assertEqual(
            json.loads(body, object_pairs_hook=lambda pairs: dict(pairs)),
            {
                "apiVersion": "local-api-internal-diagnostics/v1",
                "object": "sharedClientBoundaryAttempts",
                "processEpochSha256": expected.process_epoch_sha256,
                "boundedHttpOpenAttempts": expected.bounded_http_open_attempts,
                "headlessKeychainGetAttempts": expected.headless_keychain_get_attempts,
            },
        )
        self.assertNotIn(
            target,
            json.dumps(local_api_module.LOCAL_API_SCHEMA, sort_keys=True),
        )

        tcp = create_tcp_server(self.query, port=0, bearer_token=TOKEN)
        tcp_thread = start(tcp)
        try:
            tcp_port = tcp.server_address[1]
            response = raw_exchange(
                ("127.0.0.1", tcp_port),
                (
                    f"GET {target} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{tcp_port}\r\n"
                    f"Authorization: Bearer {TOKEN}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii"),
            )
            tcp_status = int(response.split(b" ", 2)[1])
        finally:
            tcp.shutdown()
            tcp.server_close()
            tcp_thread.join(2)
        self.assertEqual(tcp_status, 404)

    def test_unix_internal_boundary_keeps_strict_framing_taxonomy(self):
        target = "/_internal/v1/shared-client-boundary-attempts"
        tail = (
            b"GET /v1/health HTTP/1.1\r\n"
            b"Host: localhost\r\nConnection: close\r\n\r\n"
        )
        cases = (
            (
                b"Host: localhost\r\nContent-Length: 1\r\n\r\nx",
                b"413",
                b"request_body_not_allowed",
            ),
            (
                b"Host: localhost\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
                b"413",
                b"request_body_not_allowed",
            ),
            (
                b"Host: localhost\r\nHost: duplicate\r\n\r\n",
                b"400",
                b"invalid_header",
            ),
            (
                b"Host: localhost\r\nContent-Length: nope\r\n\r\n",
                b"400",
                b"invalid_header",
            ),
        )
        for headers, status, code in cases:
            request = b"GET " + target.encode("ascii") + b" HTTP/1.1\r\n" + headers
            with self.subTest(status=status, code=code), patch(
                "openusage_bar.local_api.shared_client_boundary_attempt_counters"
            ) as snapshot:
                response = raw_exchange(self.socket_path, request + tail)
            self.assertTrue(response.startswith(b"HTTP/1.1 " + status + b" "))
            self.assertIn(b"Connection: close\r\n", response)
            self.assertEqual(response.count(b"HTTP/1.1 "), 1)
            self.assertIn(code, response)
            self.assertNotIn(b"sharedClientBoundaryAttempts", response)
            snapshot.assert_not_called()

    def test_routes_reuse_canonical_query_envelopes(self):
        expectations = {
            "/v1/summary?today=2026-07-14": "todayTokens",
            "/v1/snapshot?today=2026-07-14": "quotaWindows",
            "/v1/capacity": "providers",
            "/v1/activity/daily?from=2026-07-14&to=2026-07-14": "rows",
            "/v1/costs/daily?from=2026-07-14&to=2026-07-14": "rows",
            "/v1/quotas/history?limit=10": "snapshots",
            "/v1/sources/status": "sources",
            "/v1/changes?after=0&limit=10": "records",
            "/v1/capabilities": "providers",
            "/v1/providers": "providers",
            "/v1/health": "health",
            "/v1/schema": "routes",
            "/v1/schema.json": "schema",
            "/schema": "routes",
        }
        for target, field in expectations.items():
            with self.subTest(target=target):
                status, headers, body = self.request(target)
                self.assertEqual(status, 200)
                payload = json.loads(body)
                self.assertEqual(payload["schemaVersion"], "1.0")
                self.assertIn("dataRevision", payload)
                self.assertIn(field, payload)
                self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertNotIn("access-control-allow-origin", headers)

    def test_missing_today_is_null_on_summary_and_snapshot_routes(self):
        for target, value_path in (
            ("/v1/summary?today=2026-07-15", ("todayTokens",)),
            ("/v1/snapshot?today=2026-07-15", ("summary", "todayTokens")),
        ):
            with self.subTest(target=target):
                status, _, body = self.request(target)
                self.assertEqual(status, 200)
                value = json.loads(body)
                for key in value_path:
                    value = value[key]
                self.assertIsNone(value)

    def test_machine_schema_route_serves_the_committed_draft(self):
        status, _, body = self.request("/v1/schema.json")
        payload = json.loads(body)
        committed = json.loads(
            (Path(__file__).parents[1] / "openusage_bar/resources/local-api-v1.schema.json")
            .read_text(encoding="utf-8")
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["schema"], committed)
        self.assertEqual(payload["schema"]["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_snapshot_api_and_cli_are_exactly_identical(self):
        status, _, body = self.request("/v1/snapshot?today=2026-07-14")
        stdout, stderr = io.StringIO(), io.StringIO()
        code = collector_main(
            [
                "snapshot", "--today", "2026-07-14",
                "--format", "json", "--offline",
            ],
            stdout=stdout,
            stderr=stderr,
            store=self.store,
            query=self.query,
            clock=lambda: NOW,
        )

        self.assertEqual((status, code, stderr.getvalue()), (200, 0, ""))
        self.assertEqual(json.loads(body), json.loads(stdout.getvalue()))

    def test_activity_route_exposes_token_counting_convention(self):
        status, _, body = self.request(
            "/v1/activity/daily?from=2026-07-14&to=2026-07-14"
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body)["rows"][0]["tokenCountingConvention"],
            "unknown",
        )

    def test_costs_route_filters_provider_and_currency_and_rejects_bad_parameters(self):
        status, _, body = self.request(
            "/v1/costs/daily?from=2026-07-14&to=2026-07-14&providerIds=openai&currencies=USD"
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["rows"][0]["amount"], "12.34")
        self.assertTrue(payload["coverage"][0]["covered"])

        for target in (
            "/v1/costs/daily?from=2026-07-14",
            "/v1/costs/daily?from=2026-07-14&to=2026-07-14&unknown=1",
            "/v1/costs/daily?from=2026-07-14&from=2026-07-13&to=2026-07-14",
            "/v1/costs/daily?from=bad&to=2026-07-14",
            "/v1/costs/daily?from=2026-07-14&to=2026-07-14&currencies=usd",
        ):
            with self.subTest(target=target):
                status, _, body = self.request(target)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(body))

    def test_capabilities_are_complete_canonical_nonlocalized_family_contract(self):
        status, _, body = self.request("/v1/capabilities")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload["providers"]), 39)
        self.assertEqual(
            [row["familyId"] for row in payload["providers"]],
            sorted(row["familyId"] for row in payload["providers"]),
        )
        self.assertEqual(payload["upstream"], {
            "name": "openusage", "version": "0.23.0", "revision": "3059f1b",
            "familyCount": 35,
        })
        self.assertEqual(payload["observerPlatform"], {
            "operatingSystem": "macos",
            "support": "supported",
            "supportedSourceCount": 50,
            "totalSourceCount": 50,
            "reasonCode": "supported_sources_available",
        })
        for family in payload["providers"]:
            self.assertEqual(set(family), {
                "providerId", "familyId", "displayName", "category",
                "metricFamilies",
                "regions", "supportsAccounts", "capabilities", "sources",
            })
            self.assertEqual(family["providerId"], family["familyId"])
            self.assertTrue(family["metricFamilies"])
            self.assertEqual(set(family["capabilities"]), {
                "quotaWindows", "tokenHistory", "modelBreakdown",
                "resetTimestamps", "billing", "credits", "balance", "cost",
                "rateLimits", "serviceStatus",
            })
            self.assertEqual(
                set(family["capabilities"]["quotaWindows"]),
                {"state", "values"},
            )
            for source in family["sources"]:
                self.assertEqual(set(source), {
                    "sourceId", "kind", "timeoutSeconds", "freshnessSeconds",
                    "credentialType", "requiresCredential", "operatingSystems",
                    "stability", "provenance", "factFamilies", "authority",
                    "accountScope", "modelScope", "verification",
                    "platformSupport",
                })
                self.assertEqual(source["platformSupport"], {
                    "state": "supported",
                    "reasonCode": "supported_sources_available",
                })
        lowered = json.dumps(payload, ensure_ascii=False).lower()
        for localized in ("正常", "错误", "未配置", "过期"):
            self.assertNotIn(localized, lowered)

    def test_capabilities_expose_known_facts_and_preserve_unknown_semantics(self):
        _, _, body = self.request("/v1/capabilities")
        providers = {
            provider["familyId"]: provider
            for provider in json.loads(body)["providers"]
        }

        self.assertEqual(providers["codex"]["capabilities"], {
            "quotaWindows": {
                "state": "supported", "values": ["five_hour", "weekly"],
            },
            "tokenHistory": "supported",
            "modelBreakdown": "supported",
            "resetTimestamps": "supported",
            "billing": "unknown",
            "credits": "unknown",
            "balance": "unknown",
            "cost": "unknown",
            "rateLimits": "unknown",
            "serviceStatus": "unknown",
        })
        self.assertEqual(
            providers["kiro_cli"]["capabilities"]["quotaWindows"],
            {"state": "supported", "values": ["billing_cycle"]},
        )
        self.assertEqual(
            providers["kiro_cli"]["capabilities"]["credits"], "supported"
        )
        self.assertEqual(
            providers["step_plan"]["capabilities"]["billing"], "unknown"
        )
        self.assertEqual(
            providers["step_plan"]["sources"][0], {
                "sourceId": "step_plan_browser_session",
                "kind": "browser_session",
                "timeoutSeconds": 12,
                "freshnessSeconds": 300,
                "credentialType": "browser_session",
                "requiresCredential": True,
                "operatingSystems": ["macos"],
                "stability": "experimental",
                "provenance": "user_session",
                "factFamilies": ["detection", "subscription_capacity"],
                "authority": "provider_official",
                "accountScope": "configured_account",
                "modelScope": "aggregate",
                "verification": "live_account",
                "platformSupport": {
                    "state": "supported",
                    "reasonCode": "supported_sources_available",
                },
            },
        )
        openusage = providers["codex"]["sources"][1]
        self.assertEqual(
            (openusage["sourceId"], openusage["operatingSystems"],
             openusage["stability"], openusage["provenance"]),
            ("openusage", ["macos"], "pinned", "openusage_upstream"),
        )

        self.assertEqual(
            providers["openai"]["capabilities"]["quotaWindows"],
            {"state": "unknown", "values": []},
        )
        self.assertNotEqual(
            providers["openai"]["capabilities"]["quotaWindows"]["state"],
            "unsupported",
        )
        for provider in providers.values():
            quota = provider["capabilities"]["quotaWindows"]
            if quota["state"] == "supported":
                self.assertTrue(quota["values"])
            else:
                self.assertIn(quota["state"], {"unknown", "unsupported"})
                self.assertEqual(quota["values"], [])

    def test_capabilities_do_not_expose_private_credential_or_account_fields(self):
        _, _, body = self.request("/v1/capabilities")
        payload = json.loads(body)
        keys = set()

        def collect(value):
            if isinstance(value, dict):
                keys.update(value)
                for nested in value.values():
                    collect(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested)

        collect(payload)
        self.assertIn("tokenHistory", keys)
        self.assertTrue({
            "credentialScope", "credentialScopes", "path", "paths", "token",
            "tokens", "account", "accounts", "accountRef", "raw", "rawPayload",
        }.isdisjoint(keys))

    def test_weak_etag_ignores_only_generated_at(self):
        status, headers, body = self.request("/v1/providers")
        self.assertEqual(status, 200)
        self.assertTrue(headers["etag"].startswith('W/"'))
        first = json.loads(body)

        self.query.clock = lambda: NOW + timedelta(seconds=30)
        status, advanced_headers, advanced_body = self.request("/v1/providers")
        self.assertEqual(status, 200)
        advanced = json.loads(advanced_body)
        self.assertNotEqual(first["generatedAt"], advanced["generatedAt"])
        self.assertEqual(headers["etag"], advanced_headers["etag"])
        first.pop("generatedAt")
        advanced.pop("generatedAt")
        self.assertEqual(first, advanced)

        status, cached_headers, cached_body = self.request(
            "/v1/providers", headers={"If-None-Match": headers["etag"]}
        )
        self.assertEqual((status, cached_body), (304, b""))
        self.assertEqual(cached_headers["etag"], headers["etag"])

    def test_capability_etag_changes_for_every_capability_and_source_metadata_mutation(self):
        _, baseline_headers, _ = self.request("/v1/capabilities")
        baseline = baseline_headers["etag"]
        first = registry.descriptors[0]
        mutations = [(
            "quota_windows",
            replace(
                first,
                capabilities=replace(
                    first.capabilities,
                    quota_windows=QuotaWindowCapability(
                        CapabilityState.SUPPORTED, (QuotaWindow.MONTHLY,)
                    ),
                ),
            ),
        )]
        for field in (
            "token_history", "model_breakdown", "reset_timestamps", "billing",
            "credits", "balance", "cost", "rate_limits", "service_status",
        ):
            current = getattr(first.capabilities, field)
            changed = (
                CapabilityState.UNKNOWN
                if current is not CapabilityState.UNKNOWN
                else CapabilityState.SUPPORTED
            )
            mutations.append((
                field,
                replace(
                    first,
                    capabilities=replace(first.capabilities, **{field: changed}),
                ),
            ))
        source_mutations = {
            "operating_systems": frozenset({
                *first.sources[0].operating_systems, OperatingSystem.LINUX,
            }),
            "stability": SourceStability.STABLE,
            "provenance": SourceProvenance.PROVIDER_OFFICIAL,
            "fact_families": frozenset({SourceFactFamily.DETECTION}),
            "authority": SourceAuthority.UNKNOWN,
            "account_scope": AccountScope.UNKNOWN,
            "model_scope": ModelScope.UNKNOWN,
            "verification": SourceVerification.UNVERIFIED,
        }
        for field, changed in source_mutations.items():
            mutations.append((
                field,
                replace(
                    first,
                    sources=(
                        replace(first.sources[0], **{field: changed}),
                        *first.sources[1:],
                    ),
                ),
            ))

        for label, descriptor in mutations:
            with self.subTest(mutation=label):
                self.server.router.provider_registry = ProviderRegistry(
                    (descriptor, *registry.descriptors[1:])
                )
                _, headers, _ = self.request("/v1/capabilities")
                self.assertNotEqual(headers["etag"], baseline)

        self.server.router.provider_registry = registry
        with patch(
            "openusage_bar.local_api.default_catalog",
            replace(catalog, upstream_revision="updated-revision"),
        ):
            _, headers, _ = self.request("/v1/capabilities")
        self.assertNotEqual(headers["etag"], baseline)

    def test_provider_instance_semantic_change_changes_etag(self):
        _, baseline_headers, _ = self.request("/v1/providers")
        self.store.upsert_provider_instance(ProviderInstance(
            provider_id="minimax-primary", family_id="minimax",
            display_name="MiniMax corrected", category="subscription",
            credential_source="minimax_builtin_api", source_kind="builtin_api",
            observed_at="2026-07-14T09:05:00Z",
        ))
        _, changed_headers, _ = self.request("/v1/providers")
        self.assertNotEqual(changed_headers["etag"], baseline_headers["etag"])

    def test_provider_instances_filter_head_etag_and_privacy_contract(self):
        status, headers, body = self.request("/v1/providers?providerIds=minimax-primary")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload["providers"]), 1)
        self.assertEqual(set(payload["providers"][0]), {
            "providerId", "familyId", "displayName", "category",
            "credentialSource", "sourceKind", "observedAt", "revision",
        })
        serialized = json.dumps(payload).lower()
        for forbidden in (
            "email", "account", "path", "token", "cookie", "payloadhash",
            "payload_hash", "change_seq", "record_type", "raw", "attributes",
        ):
            self.assertNotIn(forbidden, serialized)
        head_status, head_headers, head_body = self.request(
            "/v1/providers?providerIds=minimax-primary", method="HEAD"
        )
        self.assertEqual(head_status, 200)
        self.assertEqual(head_headers["etag"], headers["etag"])
        self.assertEqual(head_headers["content-length"], headers["content-length"])
        self.assertEqual(head_body, b"")
        cached_status, _, cached_body = self.request(
            "/v1/providers?providerIds=minimax-primary",
            headers={"If-None-Match": headers["etag"]},
        )
        self.assertEqual((cached_status, cached_body), (304, b""))

    def test_provider_instances_use_catalog_brand_without_changing_identity(self):
        self.store.upsert_provider_instance(ProviderInstance(
            provider_id="minimax-generated", family_id="minimax",
            display_name="minimax", category="subscription",
            credential_source="minimax_builtin_api", source_kind="builtin_api",
            observed_at="2026-07-14T09:00:00Z",
        ))
        self.store.upsert_provider_instance(ProviderInstance(
            provider_id="mistral-custom", family_id="mistral",
            display_name="miſtral", category="api",
            credential_source="openusage", source_kind="openusage",
            observed_at="2026-07-14T09:01:00Z",
        ))

        status, _, body = self.request(
            "/v1/providers?providerIds=minimax-generated,mistral-custom"
        )
        providers = {
            item["providerId"]: item for item in json.loads(body)["providers"]
        }
        provider = providers["minimax-generated"]

        self.assertEqual(status, 200)
        self.assertEqual(provider["providerId"], "minimax-generated")
        self.assertEqual(provider["familyId"], "minimax")
        self.assertEqual(provider["displayName"], "MiniMax")
        self.assertEqual(providers["mistral-custom"]["displayName"], "miſtral")

    def test_provider_id_set_semantics_canonicalize_body_and_etag(self):
        targets = (
            "/v1/providers?providerIds=minimax-primary,zfuture",
            "/v1/providers?providerIds=zfuture,minimax-primary",
            "/v1/providers?providerIds=zfuture,minimax-primary,zfuture",
        )
        responses = [self.request(target) for target in targets]
        self.assertTrue(all(status == 200 for status, _, _ in responses))
        self.assertEqual(len({body for _, _, body in responses}), 1)
        self.assertEqual(len({headers["etag"] for _, headers, _ in responses}), 1)

        etag = responses[0][1]["etag"]
        status, head_headers, body = self.request(targets[1], method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(head_headers["etag"], etag)
        status, headers, body = self.request(
            targets[2], headers={"If-None-Match": etag}
        )
        self.assertEqual((status, body), (304, b""))
        self.assertEqual(headers["etag"], etag)

        unfiltered = self.request("/v1/providers")
        explicit_empty = self.request("/v1/providers?providerIds=")
        self.assertEqual(unfiltered[1]["etag"], explicit_empty[1]["etag"])
        self.assertEqual(unfiltered[2], explicit_empty[2])

    def test_provider_instances_reject_invalid_id_without_echo(self):
        secret = "bad%2FSECRET"
        status, _, body = self.request(f"/v1/providers?providerIds={secret}")
        self.assertEqual(status, 400)
        self.assertNotIn(secret.encode(), body)

    def test_schema_includes_provider_route(self):
        status, _, body = self.request("/v1/schema")
        self.assertEqual(status, 200)
        self.assertIn("/v1/providers", json.loads(body)["routes"])

    def test_head_has_get_headers_without_a_body(self):
        get_status, get_headers, _ = self.request("/v1/capacity")
        status, headers, body = self.request("/v1/capacity", method="HEAD")
        self.assertEqual((get_status, status), (200, 200))
        self.assertEqual(headers["etag"], get_headers["etag"])
        self.assertEqual(headers["content-length"], get_headers["content-length"])
        self.assertEqual(body, b"")

    def test_etag_is_stable_and_if_none_match_returns_empty_304(self):
        status, headers, body = self.request("/v1/summary?today=2026-07-14")
        self.assertEqual(status, 200)
        self.assertTrue(body)
        status, cached_headers, body = self.request(
            "/v1/summary?today=2026-07-14", headers={"If-None-Match": headers["etag"]}
        )
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(cached_headers["etag"], headers["etag"])
        self.assertNotIn("content-length", cached_headers)

    def test_if_none_match_uses_weak_list_and_wildcard_semantics_for_get_and_head(self):
        target = "/v1/providers"
        _, headers, _ = self.request(target)
        weak = headers["etag"]
        strong = weak[2:]
        validators = (strong, f'"un,related", {weak}', "*")
        for method in ("GET", "HEAD"):
            for validator in validators:
                with self.subTest(method=method, validator=validator):
                    status, cached_headers, body = self.request(
                        target,
                        method=method,
                        headers={"If-None-Match": validator},
                    )
                    self.assertEqual((status, body), (304, b""))
                    self.assertEqual(cached_headers["etag"], weak)
                    self.assertNotIn("content-length", cached_headers)

        status, _, body = self.request(
            target, headers={"If-None-Match": '"different"'}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body)

    def test_invalid_or_duplicate_if_none_match_is_sanitized(self):
        for invalid in ('W/ "broken"', '*, "other"', '"unterminated'):
            with self.subTest(invalid=invalid):
                status, _, body = self.request(
                    "/v1/providers", headers={"If-None-Match": invalid}
                )
                self.assertEqual(status, 400)
                self.assertEqual(
                    json.loads(body)["error"]["code"], "invalid_header"
                )
                self.assertNotIn(invalid.encode(), body)

        response = raw_exchange(
            self.socket_path,
            b"GET /v1/providers HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"If-None-Match: \"one\"\r\n"
            b"If-None-Match: \"two\"\r\n\r\n",
        )
        status, _, body = split_raw_response(response)
        self.assertTrue(status.startswith(b"HTTP/1.1 400 "))
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_header")

    def test_origin_is_absent_or_exactly_allowlisted(self):
        status, _, _ = self.request("/v1/health", headers={"Origin": "https://evil.test"})
        self.assertEqual(status, 403)

    def test_methods_and_bodies_are_rejected_without_route_execution(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            with self.subTest(method=method):
                status, headers, body = self.request("/v1/health", method=method)
                self.assertEqual(status, 405)
                self.assertEqual(headers["allow"], "GET, HEAD")
                self.assertEqual(json.loads(body)["error"]["code"], "method_not_allowed")
        status, _, body = self.request(
            "/v1/health", headers={"Content-Length": "1"}, body=b"x"
        )
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["error"]["code"], "request_body_not_allowed")
        status, _, body = self.request(
            "/v1/health", method="POST", headers={"Content-Length": "1"}, body=b"x"
        )
        self.assertEqual(status, 405)
        self.assertEqual(json.loads(body)["error"]["code"], "method_not_allowed")

    def test_strict_boundary_validation_has_stable_sanitized_errors(self):
        targets = (
            "/v1/activity/daily?from=2026-07-14&to=2026-07-14&unknown=1",
            "/v1/activity/daily?from=2026-07-14&from=2026-07-13&to=2026-07-14",
            "/v1/activity/daily?from=2026-01-01&to=2028-12-31",
            "/v1/activity/daily?from=bad&to=2026-07-14",
            "/v1/activity/daily?from=2026-07-14&to=2026-07-14&providerIds=bad/id",
            "/v1/capacity?limit=1001",
            "/v1/changes?after=-1",
            "/v1/changes?after=999999999999999999999999999999999999",
            "/v1/changes?after=999",
            "/v1/changes?after=%ZZ",
            "/v1/health?x=%00",
            "/v1/quotas/history?providerId=",
            "/v1/quotas/history?accountRef=",
        )
        for target in targets:
            with self.subTest(target=target):
                status, headers, body = self.request(target)
                self.assertEqual(status, 400)
                payload = json.loads(body)
                self.assertEqual(set(payload), {"error"})
                self.assertEqual(set(payload["error"]), {"code", "message"})
                self.assertNotIn(self.temp.name, body.decode())
                self.assertEqual(headers["cache-control"], "no-store")

    def test_request_line_header_count_and_parser_errors_are_bounded_json(self):
        requests = (
            b"GET /" + (b"a" * 8_192) + b" HTTP/1.1\r\nHost: localhost\r\n\r\n",
            b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\n" +
            b"".join(f"X-{index}: x\r\n".encode("ascii") for index in range(110)) + b"\r\n",
            b"GET /v1/health HTTP/99.0\r\nHost: localhost\r\n\r\n",
        )
        for request in requests:
            with self.subTest(prefix=request[:30]):
                status, headers, body = unix_raw_request(self.socket_path, request)
                self.assertIn(status, {400, 413})
                self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
                self.assertIn(json.loads(body)["error"]["code"], {"invalid_request", "request_too_large"})

    def test_parser_level_head_errors_have_representation_length_without_wire_body(self):
        oversized_headers = b"".join(
            f"X-{index}: x\r\n".encode("ascii") for index in range(110)
        )
        requests = (
            b"HEAD /v1/health HTTP/1.1\r\nHost: localhost\r\n" + oversized_headers + b"\r\n",
            b"HEAD /v1/health HTTP/1.1\r\nHost: localhost\r\nX-Large: " + b"x" * 65_537 + b"\r\n\r\n",
            b"HEAD /v1/health HTTP/99.0\r\nHost: localhost\r\n\r\n",
        )
        for request in requests:
            with self.subTest(prefix=request[:50]):
                status, headers, body = split_raw_response(raw_exchange(self.socket_path, request))
                self.assertTrue(status.startswith((b"HTTP/1.1 400 ", b"HTTP/1.1 413 ")))
                self.assertGreater(int(headers["content-length"]), 0)
                self.assertEqual(body, b"")

        status, headers, body = split_raw_response(raw_exchange(
            self.socket_path,
            b"GET /v1/health HTTP/99.0\r\nHost: localhost\r\n\r\n",
        ))
        self.assertTrue(status.startswith(b"HTTP/1.1 400 "))
        self.assertEqual(len(body), int(headers["content-length"]))
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")

    def test_oversized_request_line_preserves_strict_head_wire_semantics(self):
        target = b"/" + b"a" * 8_192
        for method, expects_body in ((b"HEAD", False), (b"GET", True), (b"HEADX", True)):
            with self.subTest(method=method):
                response = raw_exchange(
                    self.socket_path,
                    method + b" " + target + b" HTTP/1.1\r\nHost: localhost\r\n\r\n",
                )
                status, headers, body = split_raw_response(response)
                self.assertTrue(status.startswith(b"HTTP/1.1 413 "))
                representation_length = int(headers["content-length"])
                self.assertGreater(representation_length, 0)
                if expects_body:
                    self.assertEqual(len(body), representation_length)
                    self.assertEqual(json.loads(body)["error"]["code"], "request_too_large")
                else:
                    self.assertEqual(body, b"")

    def test_http_09_10_absolute_form_and_obs_fold_are_rejected_and_closed(self):
        requests = (
            b"GET /v1/health\r\n",
            b"GET /v1/health HTTP/1.0\r\nHost: localhost\r\n\r\n",
            b"GET http://localhost/v1/health HTTP/1.1\r\nHost: localhost\r\n\r\n",
            b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\nX-Test: a\r\n folded\r\n\r\n",
        )
        for request in requests:
            with self.subTest(prefix=request[:40]):
                response = raw_exchange(str(self.socket_path), request)
                self.assertTrue(response.startswith(b"HTTP/1.1 400 "), response[:100])
                self.assertIn(b"Connection: close\r\n", response)
                self.assertEqual(response.count(b"HTTP/1.1 400 "), 1)
                self.assertNotIn(b"HTTP/1.1 200 ", response)

    def test_unknown_route_and_internal_failure_are_sanitized(self):
        status, _, body = self.request("/v1/missing")
        self.assertEqual((status, json.loads(body)["error"]["code"]), (404, "not_found"))
        original = self.query.capacity
        self.query.capacity = lambda *_: (_ for _ in ()).throw(RuntimeError("secret /tmp/sql"))
        try:
            status, _, body = self.request("/v1/capacity")
        finally:
            self.query.capacity = original
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"]["code"], "internal_error")
        self.assertNotIn("secret", body.decode())

    def test_concurrent_reads_complete(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.request("/v1/capacity")[0])) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual(results, [200] * 12)


@unittest.skipIf(sys.platform == "win32", "Windows uses loopback TCP transport")
class SocketLifecycleTests(unittest.TestCase):
    def test_symlink_regular_file_and_live_socket_are_never_clobbered(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, query = seeded_query()
            try:
                root = Path(temporary)
                target = root / "target"
                target.write_text("keep", encoding="utf-8")
                link = root / "api.sock"
                link.symlink_to(target)
                with self.assertRaises(OSError):
                    create_unix_server(link, query)
                link.unlink()
                link.write_text("keep", encoding="utf-8")
                with self.assertRaises(OSError):
                    create_unix_server(link, query)
                link.unlink()
                live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                live.bind(str(link))
                live.listen(1)
                try:
                    with self.assertRaises(OSError):
                        create_unix_server(link, query)
                finally:
                    live.close()
                    link.unlink()
            finally:
                store.close()

    def test_stale_socket_is_replaced_but_replacement_after_start_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, query = seeded_query()
            path = Path(temporary) / "api.sock"
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(path))
            stale.close()
            server = create_unix_server(path, query)
            server.socket.close()
            path.unlink()
            path.write_text("replacement", encoding="utf-8")
            with self.assertRaisesRegex(OSError, "restored an unexpected replacement"):
                server.server_close()
            self.assertEqual(path.read_text(encoding="utf-8"), "replacement")
            store.close()

    def test_cleanup_quarantine_preserves_file_and_socket_replacements(self):
        for replacement in ("file", "socket"):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as temporary:
                store, query = seeded_query()
                path = Path(temporary) / "private" / "api.sock"
                held = []

                def replace(original):
                    if replacement == "file":
                        original.write_text("replacement", encoding="utf-8")
                    else:
                        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        peer.bind(str(original))
                        peer.listen(1)
                        held.append(peer)

                server = create_unix_server(path, query, cleanup_hook=replace)
                server.server_close()
                try:
                    if replacement == "file":
                        self.assertEqual(path.read_text(encoding="utf-8"), "replacement")
                    else:
                        self.assertTrue(stat.S_ISSOCK(path.lstat().st_mode))
                finally:
                    for peer in held:
                        peer.close()
                    path.unlink(missing_ok=True)
                    store.close()


@unittest.skipIf(sys.platform == "win32", "Windows uses loopback TCP transport")
class DeadlineTests(unittest.TestCase):
    def test_absolute_deadline_evicts_slow_drip_and_releases_only_slot(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, query = seeded_query()
            path = Path(temporary) / "private" / "api.sock"
            server = create_unix_server(
                path, query, max_threads=1, client_timeout=1,
                request_deadline=0.12,
            )
            thread = start(server)
            expired = threading.Event()
            original_expire = server._expire_request

            def mark_expired(request):
                try:
                    original_expire(request)
                finally:
                    expired.set()

            server._expire_request = mark_expired
            slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            slow.connect(str(path))

            def drip():
                for byte in b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\n\r\n":
                    try:
                        slow.send(bytes([byte]))
                    except OSError:
                        return
                    time.sleep(0.04)

            dripper = threading.Thread(target=drip)
            dripper.start()
            self.assertTrue(expired.wait(2))
            deadline = time.monotonic() + 2
            while (
                server.active_deadline_count != 0
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertEqual(server.active_deadline_count, 0)
            status, _, _ = unix_request(path, "/v1/health")
            self.assertEqual(status, 200)
            dripper.join(1)
            slow.close()
            server.shutdown()
            server.server_close()
            thread.join(2)
            self.assertEqual(server.active_deadline_count, 0)
            store.close()

    def test_shutdown_cancels_deadline_and_interrupts_active_reader(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, query = seeded_query()
            path = Path(temporary) / "private" / "api.sock"
            server = create_unix_server(path, query, max_threads=1, request_deadline=10)
            thread = start(server)
            slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            slow.connect(str(path))
            slow.sendall(b"G")
            deadline = time.monotonic() + 1
            while server.active_deadline_count == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(server.active_deadline_count, 1)
            server.shutdown()
            server.server_close()
            thread.join(2)
            slow.close()
            self.assertEqual(server.active_deadline_count, 0)
            store.close()

    def test_close_cannot_join_registered_timer_before_it_starts(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, query = seeded_query()
            path = Path(temporary) / "private" / "api.sock"
            server = create_unix_server(
                path, query, max_threads=1, request_deadline=0.2,
            )
            thread = start(server)
            registered = threading.Event()
            allow_start = threading.Event()
            expired = threading.Event()
            original_start = threading.Timer.start
            original_expire = server._expire_request

            def mark_timer_expiry(request):
                if isinstance(threading.current_thread(), threading.Timer):
                    expired.set()
                return original_expire(request)

            server._expire_request = mark_timer_expiry

            def blocked_start(timer):
                registered.set()
                self.assertTrue(allow_start.wait(2))
                return original_start(timer)

            slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            with patch("openusage_bar.local_api.threading.Timer.start", blocked_start):
                slow.connect(str(path))
                slow.sendall(b"G")
                self.assertTrue(registered.wait(1))
                server.shutdown()
                close_errors = []
                closer = threading.Thread(
                    target=lambda: _capture_error(server.server_close, close_errors)
                )
                closer.start()
                time.sleep(0.05)
                allow_start.set()
                closer.join(2)

            slow.close()
            thread.join(2)
            time.sleep(0.3)
            self.assertEqual(close_errors, [])
            self.assertFalse(closer.is_alive())
            self.assertEqual(server.active_deadline_count, 0)
            self.assertFalse(path.exists())
            self.assertFalse(expired.is_set())
            store.close()


class LocalAPITokenPublicationTests(unittest.TestCase):
    def test_token_path_validation_uses_the_target_platform_path_flavor(self):
        windows_token_path = (
            r"C:\Users\example\AppData\Local\openusage-bar\api.token"
        )
        with (
            patch.object(local_api_module.os, "name", "nt"),
            patch.object(local_api_module, "Path", PureWindowsPath),
        ):
            self.assertEqual(
                local_api_module._validated_token_path(windows_token_path),
                PureWindowsPath(windows_token_path),
            )
            invalid_windows_paths = (
                r"\\server\share\api.token",
                r"\\?\C:\state\api.token",
                r"\\.\C:\state\api.token",
                "C:\\",
                r"C:\state\..\api.token",
                r"C:\state\api.token:stream",
                r"C:\state:cache\api.token",
                "C:\\state\\api\x00.token",
                "C:\\state\\api\n.token",
                r"state\api.token",
                r"\state\api.token",
                r"C:state\api.token",
            )
            for candidate in invalid_windows_paths:
                with self.subTest(platform="win32", candidate=repr(candidate)):
                    with self.assertRaisesRegex(
                        ValueError,
                        "^token path must be absolute$",
                    ):
                        local_api_module._validated_token_path(candidate)

        posix_token_path = "/home/example/.local/state/openusage-bar/api.token"
        with (
            patch.object(local_api_module.os, "name", "posix"),
            patch.object(local_api_module, "Path", PurePosixPath),
        ):
            self.assertEqual(
                local_api_module._validated_token_path(posix_token_path),
                PurePosixPath(posix_token_path),
            )

    def test_relative_token_path_is_rejected_before_filesystem_mutation(self):
        store, query = seeded_query()
        mkdir_calls: list[Path] = []
        hardening_calls: list[Path] = []

        class GuardWindowsFileSecurity:
            def harden_directory(self, directory: Path) -> None:
                hardening_calls.append(directory)

            def harden_file(self, _descriptor: int) -> None:
                raise AssertionError("a token file must not be opened")

        def forbidden_mkdir(candidate, *args, **kwargs):
            del args, kwargs
            mkdir_calls.append(Path(candidate))
            raise AssertionError("mkdir ran before token path validation")

        try:
            with (
                patch.object(
                    local_api_module,
                    "_WINDOWS_FILE_SECURITY",
                    GuardWindowsFileSecurity(),
                ),
                patch.object(local_api_module.Path, "mkdir", forbidden_mkdir),
                self.assertRaises(ValueError) as caught,
            ):
                create_tcp_server(
                    query,
                    port=0,
                    bearer_token=TOKEN,
                    token_path=Path("relative-state") / "api.token",
                )

            self.assertEqual(str(caught.exception), "token path must be absolute")
            self.assertEqual(mkdir_calls, [])
            self.assertEqual(hardening_calls, [])
        finally:
            store.close()

    def test_parent_traversal_token_path_is_rejected_before_filesystem_mutation(self):
        store, query = seeded_query()
        mkdir_calls: list[Path] = []
        hardening_calls: list[Path] = []

        class GuardWindowsFileSecurity:
            def harden_directory(self, directory: Path) -> None:
                hardening_calls.append(directory)

            def harden_file(self, _descriptor: int) -> None:
                raise AssertionError("a token file must not be opened")

        def forbidden_mkdir(candidate, *args, **kwargs):
            del args, kwargs
            mkdir_calls.append(Path(candidate))
            raise AssertionError("mkdir ran before token path validation")

        try:
            with tempfile.TemporaryDirectory() as temporary:
                token_path = (
                    Path(temporary) / "private" / ".." / "api.token"
                )
                with (
                    patch.object(
                        local_api_module,
                        "_WINDOWS_FILE_SECURITY",
                        GuardWindowsFileSecurity(),
                    ),
                    patch.object(local_api_module.Path, "mkdir", forbidden_mkdir),
                    self.assertRaises(ValueError) as caught,
                ):
                    create_tcp_server(
                        query,
                        port=0,
                        bearer_token=TOKEN,
                        token_path=token_path,
                    )

                self.assertEqual(
                    str(caught.exception),
                    "token path must be absolute",
                )
                self.assertEqual(mkdir_calls, [])
                self.assertEqual(hardening_calls, [])
                self.assertFalse((Path(temporary) / "private").exists())
        finally:
            store.close()

    def test_windows_security_seam_hardens_parent_and_open_file_before_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            token_path = Path(temporary) / "api.token"
            calls: list[tuple[str, object]] = []
            real_write = os.write

            class FakeWindowsFileSecurity:
                def harden_directory(self, directory: Path) -> None:
                    calls.append(("directory", directory))

                def harden_file(self, descriptor: int) -> None:
                    calls.append(("file", descriptor))

                def verify_file(self, _descriptor: int) -> None:
                    raise AssertionError("a new token must be hardened, not verified")

            def checked_write(descriptor: int, content: object) -> int:
                self.assertEqual(
                    calls,
                    [
                        ("directory", token_path.parent),
                        ("file", descriptor),
                    ],
                )
                return real_write(descriptor, content)

            with (
                patch.object(
                    local_api_module,
                    "_WINDOWS_FILE_SECURITY",
                    FakeWindowsFileSecurity(),
                ),
                patch.object(local_api_module.os, "write", checked_write),
            ):
                loaded = local_api_module._load_or_create_token(token_path, TOKEN)

            self.assertEqual(loaded, TOKEN)
            self.assertEqual(calls[0], ("directory", token_path.parent))
            self.assertEqual(calls[1][0], "file")
            self.assertEqual(len(calls), 2)

    def test_windows_existing_acl_failure_prevents_read_or_hardening(self):
        secret_detail = "security-detail-must-not-escape"
        with tempfile.TemporaryDirectory() as temporary:
            token_path = Path(temporary) / "api.token"
            token_path.write_text(TOKEN, encoding="ascii")
            if os.name != "nt":
                token_path.chmod(0o600)
            calls: list[tuple[str, object]] = []

            class FailingWindowsFileSecurity:
                def harden_directory(self, directory: Path) -> None:
                    calls.append(("directory", directory))

                def harden_file(self, descriptor: int) -> None:
                    calls.append(("harden_file", descriptor))
                    raise OSError("an existing token ACL must never be rewritten")

                def verify_file(self, descriptor: int) -> None:
                    calls.append(("verify_file", descriptor))
                    raise OSError(f"{secret_detail}: {token_path}")

            def forbidden_read(_descriptor: int, _count: int) -> bytes:
                raise AssertionError("token bytes were read before ACL hardening")

            with (
                patch.object(
                    local_api_module,
                    "_WINDOWS_FILE_SECURITY",
                    FailingWindowsFileSecurity(),
                ),
                patch.object(local_api_module.os, "read", forbidden_read),
                self.assertRaises(OSError) as caught,
            ):
                local_api_module._load_or_create_token(token_path, None)

            self.assertEqual(str(caught.exception), "existing token file is unsafe")
            self.assertNotIn(secret_detail, str(caught.exception))
            self.assertNotIn(str(token_path), str(caught.exception))
            self.assertEqual(calls[0], ("directory", token_path.parent))
            self.assertEqual(calls[1][0], "verify_file")
            self.assertEqual(len(calls), 2)

    def test_token_path_is_published_only_after_complete_private_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            token_path = Path(temporary) / "api.token"
            write_started = threading.Event()
            allow_write = threading.Event()
            created: list[bool] = []
            errors: list[Exception] = []
            real_write = os.write

            def blocking_write(descriptor, content):
                write_started.set()
                if not allow_write.wait(2):
                    raise TimeoutError("test write was not released")
                return real_write(descriptor, content)

            def create() -> None:
                try:
                    created.append(local_api_module._create_token(token_path, TOKEN))
                except Exception as error:
                    errors.append(error)

            with patch.object(local_api_module.os, "write", blocking_write):
                worker = threading.Thread(target=create)
                worker.start()
                self.assertTrue(write_started.wait(2))
                try:
                    self.assertFalse(token_path.exists())
                finally:
                    allow_write.set()
                    worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(created, [True])
            self.assertEqual(token_path.read_text(encoding="ascii"), TOKEN)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)

    def test_failed_partial_write_leaves_no_final_token_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            token_path = Path(temporary) / "api.token"
            real_write = os.write
            calls = 0

            def partial_then_fail(descriptor, content):
                nonlocal calls
                calls += 1
                if calls == 1:
                    prefix = max(1, len(content) // 2)
                    return real_write(descriptor, content[:prefix])
                raise OSError("synthetic token write failure")

            with patch.object(local_api_module.os, "write", partial_then_fail):
                with self.assertRaisesRegex(OSError, "synthetic token write failure"):
                    local_api_module._create_token(token_path, TOKEN)

            self.assertFalse(token_path.exists())

    def test_initial_metadata_failure_leaves_only_private_empty_orphan(self):
        failure = "synthetic token metadata failure"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token_path = root / "api.token"

            with patch.object(
                local_api_module.os,
                "fstat",
                side_effect=OSError(failure),
            ):
                with self.assertRaises(OSError) as caught:
                    local_api_module._create_token(token_path, TOKEN)

            self.assertFalse(token_path.exists())
            residuals = list(root.iterdir())
            self.assertLessEqual(
                len(residuals),
                1,
                "more than one temporary token was left behind",
            )
            for residual in residuals:
                metadata = residual.lstat()
                self.assertTrue(
                    stat.S_ISREG(metadata.st_mode),
                    "residual temporary token is not regular",
                )
                self.assertEqual(
                    metadata.st_size,
                    0,
                    "residual temporary token is not empty",
                )
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
            self.assertEqual(str(caught.exception), failure)

    @unittest.skipIf(os.name == "nt", "Windows locks the open publication node")
    def test_initial_metadata_failure_preserves_replacement_node(self):
        failure = "synthetic token metadata failure"
        replacement = b"replacement-node"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token_path = root / "api.token"
            candidates: list[Path] = []
            replacement_identity: tuple[int, int] | None = None
            fstat_calls = 0
            real_open = os.open
            real_fstat = os.fstat

            def capture_candidate(target, flags, *args, **kwargs):
                descriptor = real_open(target, flags, *args, **kwargs)
                candidate = Path(target)
                if (
                    not candidates
                    and candidate.parent == root
                    and ".tmp-" in candidate.name
                ):
                    candidates.append(candidate)
                return descriptor

            def replace_then_fail(descriptor):
                nonlocal fstat_calls, replacement_identity
                fstat_calls += 1
                if fstat_calls == 1:
                    if len(candidates) != 1:
                        raise AssertionError("temporary candidate was not captured")
                    candidate = candidates[0]
                    candidate.unlink()
                    candidate.write_bytes(replacement)
                    if os.name != "nt":
                        candidate.chmod(0o600)
                    metadata = candidate.stat()
                    replacement_identity = (metadata.st_dev, metadata.st_ino)
                    raise OSError(failure)
                return real_fstat(descriptor)

            with (
                patch.object(local_api_module.os, "open", capture_candidate),
                patch.object(local_api_module.os, "fstat", replace_then_fail),
            ):
                with self.assertRaises(OSError) as caught:
                    local_api_module._create_token(token_path, TOKEN)

            self.assertEqual(str(caught.exception), failure)
            self.assertFalse(token_path.exists())
            self.assertEqual(
                len(candidates),
                1,
                "temporary candidate was not captured exactly once",
            )
            candidate = candidates[0]
            self.assertTrue(candidate.exists(), "replacement node was deleted")
            self.assertEqual(candidate.read_bytes(), replacement)
            metadata = candidate.stat()
            self.assertEqual(
                (metadata.st_dev, metadata.st_ino),
                replacement_identity,
            )

    def test_primary_write_failure_wins_over_secondary_close_failure(self):
        primary = "primary write failure"
        secondary = "secondary close failure"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token_path = root / "api.token"
            real_close = os.close
            close_calls = 0

            def fail_write(_descriptor, _content):
                raise OSError(primary)

            def close_then_fail(descriptor):
                nonlocal close_calls
                close_calls += 1
                real_close(descriptor)
                if close_calls == 1:
                    raise OSError(secondary)

            with (
                patch.object(local_api_module.os, "write", fail_write),
                patch.object(local_api_module.os, "close", close_then_fail),
            ):
                with self.assertRaises(OSError) as caught:
                    local_api_module._create_token(token_path, TOKEN)

            self.assertFalse(token_path.exists())
            self.assertFalse(any(root.iterdir()), "owned token node was not cleaned")
            self.assertEqual(str(caught.exception), primary)

    @unittest.skipIf(os.name == "nt", "directory fsync is unavailable on Windows")
    def test_parent_fsync_failure_wins_over_parent_close_failure(self):
        primary = "primary parent fsync failure"
        secondary = "secondary parent close failure"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token_path = root / "api.token"
            parent_descriptor: int | None = None
            real_fstat = os.fstat
            real_fsync = os.fsync
            real_close = os.close

            def fail_parent_fsync(descriptor):
                nonlocal parent_descriptor
                if stat.S_ISDIR(real_fstat(descriptor).st_mode):
                    parent_descriptor = descriptor
                    raise OSError(primary)
                return real_fsync(descriptor)

            def close_parent_then_fail(descriptor):
                real_close(descriptor)
                if descriptor == parent_descriptor:
                    raise OSError(secondary)

            with (
                patch.object(local_api_module.os, "fsync", fail_parent_fsync),
                patch.object(local_api_module.os, "close", close_parent_then_fail),
            ):
                with self.assertRaises(OSError) as caught:
                    local_api_module._create_token(token_path, TOKEN)

            self.assertIsNotNone(parent_descriptor, "parent fsync probe did not run")
            self.assertFalse(token_path.exists())
            self.assertFalse(any(root.iterdir()), "owned token nodes were not cleaned")
            self.assertEqual(str(caught.exception), primary)

    def test_close_failure_after_complete_write_cleans_every_token_path(self):
        failure = "synthetic token close failure"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token_path = root / "api.token"
            real_close = os.close
            calls = 0

            def close_then_fail(descriptor):
                nonlocal calls
                calls += 1
                if calls == 1:
                    real_close(descriptor)
                    raise OSError(failure)
                return real_close(descriptor)

            with patch.object(local_api_module.os, "close", close_then_fail):
                with self.assertRaises(OSError) as caught:
                    local_api_module._create_token(token_path, TOKEN)

            self.assertFalse(
                any(root.iterdir()),
                "temporary token was not cleaned",
            )
            self.assertEqual(str(caught.exception), failure)

    def test_existing_token_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            token_path = Path(temporary) / "api.token"
            existing = "e" * 48
            token_path.write_text(existing, encoding="ascii")
            if os.name != "nt":
                token_path.chmod(0o600)

            self.assertFalse(local_api_module._create_token(token_path, TOKEN))
            self.assertEqual(token_path.read_text(encoding="ascii"), existing)

    @unittest.skipUnless(hasattr(os, "link"), "hard links are unavailable")
    def test_read_token_rejects_hardlink_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.token"
            alias = root / "api.token"
            source.write_text(TOKEN, encoding="ascii")
            if os.name != "nt":
                source.chmod(0o600)
            original = source.stat()
            os.link(source, alias)

            with self.assertRaisesRegex(
                OSError,
                "^existing token file is unsafe$",
            ):
                local_api_module._read_token(alias)

            current = source.stat()
            self.assertEqual(source.read_text(encoding="ascii"), TOKEN)
            self.assertEqual((current.st_dev, current.st_ino), (original.st_dev, original.st_ino))
            self.assertEqual(current.st_nlink, 2)

    @unittest.skipUnless(hasattr(os, "link"), "hard links are unavailable")
    def test_tcp_server_rejects_hardlink_token_without_modifying_source(self):
        store, query = seeded_query()
        server = None
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source.token"
                alias = root / "api.token"
                source.write_text(TOKEN, encoding="ascii")
                if os.name != "nt":
                    source.chmod(0o600)
                original = source.stat()
                os.link(source, alias)

                with self.assertRaisesRegex(
                    OSError,
                    "^existing token file is unsafe$",
                ):
                    server = create_tcp_server(query, port=0, token_path=alias)

                current = source.stat()
                self.assertEqual(source.read_text(encoding="ascii"), TOKEN)
                self.assertEqual(
                    (current.st_dev, current.st_ino),
                    (original.st_dev, original.st_ino),
                )
                self.assertEqual(current.st_nlink, 2)
        finally:
            if server is not None:
                server.server_close()
            store.close()


class TCPLocalAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store, self.query = seeded_query()
        self.token_path = Path(self.temp.name) / "api.token"
        self.server = create_tcp_server(
            self.query, port=0, bearer_token=TOKEN, token_path=self.token_path,
            allowed_origins={"https://scheduler.local"}, clock=lambda: NOW,
        )
        self.thread = start(self.server)
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.store.close()
        self.temp.cleanup()

    def request(self, target="/v1/health", *, method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        base = {"Host": f"127.0.0.1:{self.port}", "Authorization": f"Bearer {TOKEN}"}
        base.update(headers or {})
        connection.request(method, target, headers=base)
        response = connection.getresponse()
        result = response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        connection.close()
        return result

    def test_loopback_bind_never_uses_reverse_dns(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            local_api_module.socket,
            "getfqdn",
            side_effect=AssertionError("reverse DNS is not loopback authority"),
        ):
            server = create_tcp_server(
                self.query,
                port=0,
                bearer_token=TOKEN,
                token_path=Path(directory) / "api.token",
            )
        try:
            self.assertEqual(server.server_name, "127.0.0.1")
            self.assertEqual(server.server_port, server.server_address[1])
        finally:
            server.server_close()

    def test_tcp_is_loopback_only_and_token_file_is_0600(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.token_path.stat().st_mode), 0o600)
        self.assertEqual(self.token_path.read_text(encoding="utf-8"), TOKEN)

    def test_provider_contract_matches_unix_transport_shape(self):
        status, _, body = self.request("/v1/providers")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["providers"][0]["familyId"], "minimax")
        self.assertEqual(set(payload["providers"][0]), {
            "providerId", "familyId", "displayName", "category",
            "credentialSource", "sourceKind", "observedAt", "revision",
        })

    def test_quick_connect_route_matches_advertised_schema_route(self):
        status, _, body = self.request("/v1/schema")
        self.assertEqual(status, 200)
        schema_payload = json.loads(body)
        self.assertIn("/v1/quick-connect", schema_payload["routes"])

        status, _, body = self.request("/v1/quick-connect")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["schemaVersion"], schema_payload["schemaVersion"])
        self.assertTrue(payload["providers"])
        self.assertEqual(payload["providers"], sorted(
            payload["providers"], key=lambda item: item["familyId"]
        ))
        self.assertIn("apiKeyUrl", payload["providers"][0])

    def test_tcp_provider_id_set_semantics_have_one_etag_and_body(self):
        targets = (
            "/v1/providers?providerIds=minimax-primary,zfuture",
            "/v1/providers?providerIds=zfuture,minimax-primary",
            "/v1/providers?providerIds=zfuture,minimax-primary,zfuture",
        )
        responses = [self.request(target) for target in targets]
        self.assertTrue(all(status == 200 for status, _, _ in responses))
        self.assertEqual(len({body for _, _, body in responses}), 1)
        self.assertEqual(len({headers["etag"] for _, headers, _ in responses}), 1)
        self.assertTrue(responses[0][1]["etag"].startswith('W/"'))

    def test_tcp_if_none_match_weak_list_wildcard_and_invalid_semantics(self):
        target = "/v1/providers"
        _, headers, _ = self.request(target)
        weak = headers["etag"]
        for method in ("GET", "HEAD"):
            for validator in (weak[2:], f'"other", {weak}', "*"):
                with self.subTest(method=method, validator=validator):
                    status, response_headers, body = self.request(
                        target,
                        method=method,
                        headers={"If-None-Match": validator},
                    )
                    self.assertEqual((status, body), (304, b""))
                    self.assertEqual(response_headers["etag"], weak)

        status, _, body = self.request(
            target, headers={"If-None-Match": 'W/ "invalid"'}
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_header")
        status, _, body = self.request(
            target, headers={"If-None-Match": '"different"'}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body)

    def test_tcp_requires_exact_bearer_without_leaking_it(self):
        for authorization in (None, "Bearer wrong", "Basic whatever"):
            headers = {"Authorization": authorization} if authorization else {"Authorization": ""}
            status, _, body = self.request(headers=headers)
            self.assertEqual(status, 401)
            self.assertNotIn(TOKEN.encode(), body)

    def test_tcp_parser_level_head_error_has_no_wire_body(self):
        request = (
            b"HEAD /v1/health HTTP/1.1\r\n"
            + f"Host: 127.0.0.1:{self.port}\r\n".encode("ascii")
            + b"".join(f"X-{index}: x\r\n".encode("ascii") for index in range(110))
            + b"\r\n"
        )
        status, headers, body = split_raw_response(
            raw_exchange(("127.0.0.1", self.port), request)
        )
        self.assertTrue(status.startswith(b"HTTP/1.1 413 "))
        self.assertGreater(int(headers["content-length"]), 0)
        self.assertEqual(body, b"")

        target = b"/" + b"a" * 8_192
        for method, expects_body in ((b"HEAD", False), (b"GET", True)):
            with self.subTest(oversized_method=method):
                oversized = (
                    method + b" " + target + b" HTTP/1.1\r\n"
                    + f"Host: 127.0.0.1:{self.port}\r\n\r\n".encode("ascii")
                )
                status, headers, body = split_raw_response(
                    raw_exchange(("127.0.0.1", self.port), oversized)
                )
                self.assertTrue(status.startswith(b"HTTP/1.1 413 "))
                representation_length = int(headers["content-length"])
                self.assertGreater(representation_length, 0)
                self.assertEqual(len(body), representation_length if expects_body else 0)

    def test_ambiguous_framing_and_duplicate_security_headers_close_before_tail_request(self):
        good = (
            f"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"Authorization: Bearer {TOKEN}\r\n\r\n"
        ).encode("ascii")
        prefixes = (
            f"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: 0\r\nContent-Length: 1\r\n\r\nx",
            f"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: nope\r\n\r\n",
            f"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nAuthorization: Bearer {TOKEN}\r\nTransfer-Encoding: chunked\r\nContent-Length: 0\r\n\r\n0\r\n\r\n",
            f"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nHost: evil.test\r\nAuthorization: Bearer {TOKEN}\r\n\r\n",
            f"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nAuthorization: Bearer {TOKEN}\r\nAuthorization: Bearer wrong\r\n\r\n",
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix[:60]):
                response = raw_exchange(("127.0.0.1", self.port), prefix.encode("ascii") + good)
                self.assertTrue(
                    response.startswith(b"HTTP/1.1 400 ") or response.startswith(b"HTTP/1.1 413 "),
                    response[:100],
                )
                self.assertIn(b"Connection: close\r\n", response)
                self.assertEqual(response.count(b"HTTP/1.1 "), 1)
                self.assertNotIn(b"200 OK", response)

    def test_host_rejects_dns_rebinding_nonloopback_wrong_port_and_malformed_values(self):
        for host in ("evil.test", "127.0.0.1", "127.0.0.1:1", "localhost:%s" % self.port, "[::1]:%s" % self.port):
            with self.subTest(host=host):
                status, _, body = self.request(headers={"Host": host})
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body)["error"]["code"], "forbidden_host")

    def test_allowlisted_origin_is_echoed_without_wildcard(self):
        status, headers, _ = self.request(headers={"Origin": "https://scheduler.local"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["access-control-allow-origin"], "https://scheduler.local")
        self.assertNotEqual(headers["access-control-allow-origin"], "*")
        status, _, _ = self.request(headers={"Origin": "https://evil.test"})
        self.assertEqual(status, 403)

    def test_low_entropy_token_is_rejected_and_generation_is_secure(self):
        with self.assertRaises(ValueError):
            create_tcp_server(self.query, port=0, bearer_token="short")
        with self.assertRaises(ValueError):
            create_tcp_server(self.query, port=0)
        with self.assertRaises(ValueError):
            create_tcp_server(self.query, port=0, bearer_token=TOKEN, allowed_origins={"*"})
        generated_path = Path(self.temp.name) / "generated.token"
        server = create_tcp_server(self.query, port=0, token_path=generated_path)
        try:
            self.assertGreaterEqual(len(server.bearer_token), 43)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(generated_path.stat().st_mode), 0o600)
        finally:
            server.server_close()

    def test_generated_token_is_reused_safely_across_server_restarts(self):
        path = Path(self.temp.name) / "reused.token"
        first = create_tcp_server(self.query, port=0, token_path=path)
        token = first.bearer_token
        first.server_close()
        second = create_tcp_server(self.query, port=0, token_path=path)
        try:
            self.assertEqual(second.bearer_token, token)
            self.assertEqual(path.read_text(encoding="utf-8"), token)
            thread = start(second)
            port = second.server_address[1]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            connection.request("GET", "/v1/health", headers={
                "Host": f"127.0.0.1:{port}",
                "Authorization": f"Bearer {token}",
            })
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
            second.shutdown()
            thread.join(2)
        finally:
            second.server_close()

    @unittest.skipIf(sys.platform == "win32", "POSIX permission semantics")
    def test_unsafe_or_mismatched_existing_token_files_are_rejected(self):
        unsafe = Path(self.temp.name) / "unsafe.token"
        unsafe.write_text(TOKEN, encoding="ascii")
        unsafe.chmod(0o644)
        with self.assertRaises(OSError):
            create_tcp_server(self.query, token_path=unsafe)

        mismatch = Path(self.temp.name) / "mismatch.token"
        mismatch.write_text("x" * 48, encoding="ascii")
        mismatch.chmod(0o600)
        with self.assertRaises(OSError):
            create_tcp_server(self.query, bearer_token=TOKEN, token_path=mismatch)

        target = Path(self.temp.name) / "target.token"
        target.write_text(TOKEN, encoding="ascii")
        target.chmod(0o600)
        link = Path(self.temp.name) / "link.token"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            create_tcp_server(self.query, token_path=link)


class TCPRateLimitTests(unittest.TestCase):
    def test_burst_refill_and_concurrency_are_bounded_per_server(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, query = seeded_query()
            now = [100.0]
            server = create_tcp_server(
                query, port=0, bearer_token=TOKEN,
                token_path=Path(temporary) / "token",
                rate_limit_capacity=2,
                rate_limit_refill_per_second=1.0,
                monotonic=lambda: now[0],
            )
            thread = start(server)
            port = server.server_address[1]

            def request():
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                connection.request("GET", "/v1/health", headers={
                    "Host": f"127.0.0.1:{port}",
                    "Authorization": f"Bearer {TOKEN}",
                })
                response = connection.getresponse()
                result = response.status, dict(response.getheaders()), response.read()
                connection.close()
                return result

            self.assertEqual([request()[0], request()[0]], [200, 200])
            status, headers, body = request()
            self.assertEqual(status, 429)
            self.assertEqual(json.loads(body)["error"]["code"], "rate_limited")
            self.assertEqual(headers["Retry-After"], "1")
            now[0] += 1.0
            self.assertEqual(request()[0], 200)

            now[0] += 10.0
            results = []
            workers = [threading.Thread(target=lambda: results.append(request()[0])) for _ in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(2)
            self.assertEqual(results.count(200), 2)
            self.assertEqual(results.count(429), 6)
            server.shutdown()
            server.server_close()
            thread.join(2)
            store.close()


if __name__ == "__main__":
    unittest.main()
