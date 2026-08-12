from __future__ import annotations

import os
import hashlib
import signal
import struct
import stat
import subprocess
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest.mock import patch

if os.name != "nt":
    import pwd


@unittest.skipUnless(
    hasattr(os, "getuid") and hasattr(os, "getgid"),
    "Linux observer topology canary contracts",
)
class LinuxObserverTopologyCanaryTests(unittest.TestCase):
    def test_service_survives_owned_ui_group_stop_before_preserve(self):
        from scripts.canary_linux_observer_topology import (
            LinuxObserverTopologySummary,
            run_linux_observer_topology_canary,
        )

        appimage = "/PRIVATE/audited/OpenUsageBar.AppImage"
        from openusage_bar.local_api import LinuxLocalAPIState
        from openusage_bar.platform_services import (
            LinuxCollectorServiceAbsenceState,
            LinuxCollectorServiceState,
        )

        current_uid = os.getuid()
        current_gid = os.getgid()
        executable = Path("/opt/OpenUsageBar/openusage-collector")
        argv = b"/opt/OpenUsageBar/openusage-collector\0daemon\0"
        executable_signature = "3" * 64
        service = LinuxCollectorServiceState(
            unit_file_id="11:22",
            unit_size_bytes=321,
            unit_sha256="1" * 64,
            unit_id="openusage-bar.service",
            load_state="loaded",
            active_state="active",
            sub_state="running",
            unit_file_state="enabled",
            fragment_path=Path(
                f"/home/user/.config/systemd/user/openusage-bar.service"
            ),
            drop_in_paths=(),
            needs_reload=False,
            main_pid=4312,
            process_uid=current_uid,
            process_start_time_ticks=4100,
            process_executable=executable,
            process_executable_file_id="33:44",
            process_executable_signature_sha256=executable_signature,
            process_argv_nul=argv,
            manager_executable_authority="peer-provenance-canonical-cmdline",
        )
        cgroup = (
            "0::/user.slice/"
            f"user-{current_uid}.slice/user@{current_uid}.service/"
            "app.slice/openusage-bar.service\n"
        ).encode("ascii")
        local = LinuxLocalAPIState(
            socket_file_id="55:66",
            socket_mode=0o600,
            socket_uid=current_uid,
            peer_pid=4313,
            peer_uid=current_uid,
            peer_gid=current_gid,
            peer_parent_pid=service.main_pid,
            peer_start_time_ticks=4200,
            peer_executable_file_id=service.process_executable_file_id,
            peer_executable_signature_sha256=executable_signature,
            peer_executable_path_sha256=hashlib.sha256(
                os.fsencode(executable)
            ).hexdigest(),
            peer_argv_sha256=hashlib.sha256(argv).hexdigest(),
            peer_cgroup_sha256=hashlib.sha256(cgroup).hexdigest(),
            http_status=200,
            schema_version="1.0",
            health_ok=True,
            health_status="ok",
        )
        absence = LinuxCollectorServiceAbsenceState(
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
            manager_executable_authority="peer-provenance-canonical-cmdline",
        )
        events: list[str] = []
        absence_facts = iter((absence, absence, absence, absence))
        runtime_facts = iter(
            (
                (service, local, service),
                (service, local, service),
            )
        )

        class Lease:
            def __enter__(self):
                events.append("spawn")
                return self

            def stop(self):
                events.append("stop")

            def __exit__(self, exc_type, exc, traceback):
                events.append("close")

        def read_absence():
            events.append("absence")
            return next(absence_facts)

        def observe_runtime(*, remaining_timeout):
            self.assertGreater(remaining_timeout(), 0.0)
            before, listener, after = next(runtime_facts)
            events.extend(("service", "local", "service"))
            return before, listener, after

        def start_lease(selected: str):
            self.assertEqual(selected, appimage)
            return Lease()

        def preserve(selected: str):
            self.assertEqual(selected, appimage)
            events.append("preserve")

        with (
            patch(
                "scripts.canary_linux_observer_topology._read_absence",
                side_effect=read_absence,
            ),
            patch(
                "scripts.canary_linux_observer_topology._observe_runtime",
                side_effect=observe_runtime,
            ),
            patch(
                "scripts.canary_linux_observer_topology._start_appimage_lease",
                side_effect=start_lease,
            ),
            patch(
                "scripts.canary_linux_observer_topology._run_preserve_uninstall",
                side_effect=preserve,
            ),
            patch.object(Path, "is_absolute", return_value=True),
            patch("scripts.canary_linux_observer_topology.sys.platform", "linux"),
            patch(
                "scripts.canary_linux_observer_topology.platform.machine",
                return_value="x86_64",
            ),
        ):
            summary = run_linux_observer_topology_canary(appimage)

        self.assertEqual(
            events,
            [
                "absence",
                "absence",
                "spawn",
                "service",
                "local",
                "service",
                "stop",
                "service",
                "local",
                "service",
                "close",
                "preserve",
                "absence",
                "absence",
            ],
        )
        self.assertEqual(
            tuple(field.name for field in fields(LinuxObserverTopologySummary)),
            ("service_survived_ui_stop", "service_absent_after_preserve"),
        )
        self.assertEqual(summary, LinuxObserverTopologySummary(True, True))
        with self.assertRaises(FrozenInstanceError):
            summary.service_survived_ui_stop = False
        self.assertEqual(repr(summary), "<LinuxObserverTopologySummary closed>")
        self.assertNotIn("PRIVATE", repr(summary))

    def test_owned_appimage_lease_stops_and_reaps_before_preserve(self):
        from scripts.canary_linux_observer_topology import (
            LinuxObserverTopologySummary,
            run_linux_observer_topology_canary,
        )

        events: list[object] = []
        process = SimpleProcess(events)
        absence = self._absence_state()
        service, local = self._runtime_state()
        runtime = (service, local, service)

        with tempfile.TemporaryDirectory() as directory:
            appimage = os.path.join(directory, "OpenUsageBar.AppImage")
            with open(appimage, "wb") as stream:
                stream.write(b"audited-appimage")
            os.chmod(appimage, 0o700)
            environment = {
                "HOME": "/PRIVATE/home",
                "XDG_DATA_HOME": "/PRIVATE/data",
                "XDG_RUNTIME_DIR": "/run/user/1234",
                "DBUS_SESSION_BUS_ADDRESS":
                    "unix:path=/run/user/1234/systemd/private",
                "TMPDIR": "/PRIVATE/tmp",
                "DISPLAY": ":99",
                "XAUTHORITY": "/PRIVATE/tmp/.Xauthority",
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "PYTHONNOUSERSITE": "1",
                "APPIMAGE_EXTRACT_AND_RUN": "1",
            }

            def popen(argv, **kwargs):
                events.append(("popen", tuple(argv), kwargs))
                return process

            def killpg(pgid, selected_signal):
                events.append(("killpg", pgid, selected_signal))
                if selected_signal == 0:
                    raise ProcessLookupError

            with (
                patch(
                    "scripts.canary_linux_observer_topology._read_absence",
                    side_effect=(absence, absence, absence, absence),
                ),
                patch(
                    "scripts.canary_linux_observer_topology._observe_runtime",
                    side_effect=(runtime, runtime),
                ),
                patch(
                    "scripts.canary_linux_observer_topology._closed_environment",
                    return_value=environment,
                ),
                patch(
                    "scripts.canary_linux_observer_topology.subprocess.Popen",
                    side_effect=popen,
                ),
                patch(
                    "scripts.canary_linux_observer_topology.os.getpgid",
                    return_value=process.pid,
                ),
                patch(
                    "scripts.canary_linux_observer_topology._wait_for_reserved_leader",
                    return_value=True,
                ),
                patch(
                    "scripts.canary_linux_observer_topology._process_group_has_live_member",
                    return_value=False,
                ),
                patch(
                    "scripts.canary_linux_observer_topology.os.killpg",
                    side_effect=killpg,
                ),
                patch(
                    "scripts.canary_linux_observer_topology._run_preserve_uninstall",
                    side_effect=lambda selected: events.append(
                        ("preserve", selected)
                    ),
                ),
                patch(
                    "scripts.canary_linux_observer_topology.sys.platform",
                    "linux",
                ),
                patch(
                    "scripts.canary_linux_observer_topology.platform.machine",
                    return_value="x86_64",
                ),
            ):
                summary = run_linux_observer_topology_canary(appimage)

        self.assertEqual(summary, LinuxObserverTopologySummary(True, True))
        popen_event = next(event for event in events if event[0] == "popen")
        _, argv, kwargs = popen_event
        self.assertEqual(len(argv), 1)
        self.assertRegex(argv[0], r"^/proc/self/fd/[0-9]+$")
        self.assertEqual(kwargs["pass_fds"], (int(argv[0].rsplit("/", 1)[1]),))
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertIs(kwargs["start_new_session"], True)
        self.assertEqual(kwargs["env"], environment)
        self.assertLess(
            events.index(("wait", 5.0)),
            events.index(("preserve", appimage)),
        )
        self.assertIn(("killpg", process.pid, signal.SIGTERM), events)

    def test_runtime_drift_after_ui_stop_never_runs_preserve(self):
        from scripts.canary_linux_observer_topology import (
            LinuxObserverTopologyCanaryError,
            run_linux_observer_topology_canary,
        )

        service, local = self._runtime_state()
        from dataclasses import replace

        changed_local = replace(local, socket_file_id="77:88")
        events: list[str] = []

        class Lease:
            def __enter__(self):
                return self

            def stop(self):
                events.append("stop")

            def __exit__(self, exc_type, exc, traceback):
                events.append("close")

        absence = self._absence_state()
        with (
            patch(
                "scripts.canary_linux_observer_topology._read_absence",
                side_effect=(absence, absence),
            ),
            patch(
                "scripts.canary_linux_observer_topology._wait_for_runtime",
                side_effect=(
                    (service, local, service),
                    (service, changed_local, service),
                ),
            ),
            patch(
                "scripts.canary_linux_observer_topology._start_appimage_lease",
                return_value=Lease(),
            ),
            patch(
                "scripts.canary_linux_observer_topology._run_preserve_uninstall"
            ) as preserve,
            patch("scripts.canary_linux_observer_topology.sys.platform", "linux"),
            patch(
                "scripts.canary_linux_observer_topology.platform.machine",
                return_value="x86_64",
            ),
        ):
            with self.assertRaisesRegex(
                LinuxObserverTopologyCanaryError,
                "^Linux Observer topology canary failed$",
            ):
                run_linux_observer_topology_canary("/PRIVATE/AppImage")
        self.assertEqual(events, ["stop", "close"])
        preserve.assert_not_called()

    def test_runtime_observation_accepts_only_canonical_product_facts(self):
        from scripts.canary_linux_observer_topology import _observe_runtime

        service, local = self._runtime_state()
        peer = struct.pack("=3i", local.peer_pid, local.peer_uid, local.peer_gid)
        from openusage_bar.shared_client_boundary import (
            SharedClientBoundaryAttemptCounters,
        )
        counters = SharedClientBoundaryAttemptCounters("a" * 64, 0, 0)
        events: list[str] = []

        def read_service():
            events.append("service")
            return service

        def read_local():
            events.append("local")
            return local

        boundary_facts = iter(((peer, counters), (peer, counters)))

        def read_boundary(_path, *, remaining_timeout):
            self.assertEqual(remaining_timeout(), 0.5)
            events.append("boundary")
            return next(boundary_facts)

        def evaluate_boundary(**kwargs):
            self.assertEqual(kwargs["health_peer"], peer)
            self.assertEqual(kwargs["peer_before"], peer)
            self.assertEqual(kwargs["counters_before"], counters)
            self.assertEqual(kwargs["peer_after"], peer)
            self.assertEqual(kwargs["counters_after"], counters)
            events.append("evaluate")

        with (
            patch.dict(
                os.environ,
                {"XDG_DATA_HOME": "/PRIVATE/data"},
                clear=False,
            ),
            patch(
                "scripts.canary_linux_observer_topology.read_current_user_collector_service_state",
                side_effect=read_service,
            ) as service_reader,
            patch(
                "scripts.canary_linux_observer_topology.read_current_user_local_api_state",
                side_effect=read_local,
            ) as local_reader,
            patch(
                "scripts.canary_linux_observer_topology.read_onefile_shared_client_boundary_snapshot",
                side_effect=read_boundary,
            ) as boundary_reader,
            patch(
                "scripts.canary_linux_observer_topology.evaluate_onefile_shared_client_boundary_window",
                side_effect=evaluate_boundary,
            ) as boundary_evaluator,
        ):
            observed = _observe_runtime(remaining_timeout=lambda: 0.5)

        self.assertEqual(observed, (service, local, service))
        self.assertEqual(service_reader.call_count, 2)
        local_reader.assert_called_once_with()
        self.assertEqual(boundary_reader.call_count, 2)
        boundary_evaluator.assert_called_once()
        self.assertEqual(
            events,
            ["service", "boundary", "local", "boundary", "service", "evaluate"],
        )

    def test_cli_exposes_only_fixed_stage_status_without_output(self):
        from scripts.canary_linux_observer_topology import (
            LinuxObserverTopologyCanaryError,
            main,
        )

        with patch(
            "scripts.canary_linux_observer_topology.run_linux_observer_topology_canary",
            side_effect=LinuxObserverTopologyCanaryError("runtime-before"),
        ) as runner:
            self.assertEqual(main(("--appimage", "/PRIVATE/AppImage")), 12)
        runner.assert_called_once_with("/PRIVATE/AppImage")

    @staticmethod
    def _absence_state():
        from openusage_bar.platform_services import LinuxCollectorServiceAbsenceState

        return LinuxCollectorServiceAbsenceState(
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
            manager_executable_authority="peer-provenance-canonical-cmdline",
        )

    @staticmethod
    def _runtime_state():
        from openusage_bar.local_api import LinuxLocalAPIState
        from openusage_bar.platform_services import LinuxCollectorServiceState

        current_uid = os.getuid()
        current_gid = os.getgid()
        home = Path(pwd.getpwuid(current_uid).pw_dir)
        executable = Path(
            "/PRIVATE/data/usagehub/runtime/openusage-collector"
        )
        socket_path = home / ".local/state/openusage-bar/openusage.sock"
        argv_items = (
            str(executable),
            "daemon",
            "--interval",
            "300",
            "--api-transport",
            "unix",
            "--api-socket",
            str(socket_path),
        )
        argv = ("\0".join(argv_items) + "\0").encode("utf-8")
        signature = "3" * 64
        from openusage_bar.platform_services import systemd_unit

        unit_bytes = systemd_unit(
            interval=300,
            api_socket=str(socket_path),
            command=str(executable),
        ).encode("utf-8")
        service = LinuxCollectorServiceState(
            unit_file_id="11:22",
            unit_size_bytes=len(unit_bytes),
            unit_sha256=hashlib.sha256(unit_bytes).hexdigest(),
            unit_id="openusage-bar.service",
            load_state="loaded",
            active_state="active",
            sub_state="running",
            unit_file_state="enabled",
            fragment_path=(
                home / ".config/systemd/user/openusage-bar.service"
            ),
            drop_in_paths=(),
            needs_reload=False,
            main_pid=4312,
            process_uid=current_uid,
            process_start_time_ticks=4100,
            process_executable=executable,
            process_executable_file_id="33:44",
            process_executable_signature_sha256=signature,
            process_argv_nul=argv,
            manager_executable_authority="peer-provenance-canonical-cmdline",
        )
        cgroup = (
            "0::/user.slice/"
            f"user-{current_uid}.slice/user@{current_uid}.service/"
            "app.slice/openusage-bar.service\n"
        ).encode("ascii")
        local = LinuxLocalAPIState(
            socket_file_id="55:66",
            socket_mode=0o600,
            socket_uid=current_uid,
            peer_pid=4313,
            peer_uid=current_uid,
            peer_gid=current_gid,
            peer_parent_pid=service.main_pid,
            peer_start_time_ticks=4200,
            peer_executable_file_id=service.process_executable_file_id,
            peer_executable_signature_sha256=signature,
            peer_executable_path_sha256=hashlib.sha256(
                os.fsencode(executable)
            ).hexdigest(),
            peer_argv_sha256=hashlib.sha256(argv).hexdigest(),
            peer_cgroup_sha256=hashlib.sha256(cgroup).hexdigest(),
            http_status=200,
            schema_version="1.0",
            health_ok=True,
            health_status="ok",
        )
        return service, local


class SimpleProcess:
    def __init__(self, events):
        self.pid = 4310
        self._events = events

    def poll(self):
        self._events.append("poll")
        return None

    def wait(self, timeout):
        self._events.append(("wait", timeout))
        return -signal.SIGTERM


if __name__ == "__main__":
    unittest.main()
