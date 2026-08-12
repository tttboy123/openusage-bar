from __future__ import annotations

import io
import os
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch


class GatewayEgressTopologyCanaryTests(unittest.TestCase):
    @staticmethod
    def _closed_environment(root: str = "/PRIVATE") -> dict[str, str]:
        return {
            "HOME": os.path.join(root, "home"),
            "XDG_DATA_HOME": os.path.join(root, "data"),
            "TMPDIR": os.path.join(root, "tmp"),
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONNOUSERSITE": "1",
        }

    @staticmethod
    def _prepare_private_root(root: str) -> None:
        os.chmod(root, 0o700)
        for name in ("home", "data", "tmp"):
            path = os.path.join(root, name)
            os.mkdir(path, 0o700)
            os.chmod(path, 0o700)

    @staticmethod
    def _prepare_advise_config(root: str) -> str:
        config = os.path.join(root, "gateway.json")
        with open(config, "wb") as stream:
            stream.write(b'{"enabled":true,"mode":"advise"}')
        os.chmod(config, 0o600)
        return config

    @staticmethod
    def _prepare_gateway_token(root: str) -> str:
        token = os.path.join(root, "gateway.token")
        with open(token, "wb") as stream:
            stream.write(b"g" * 48)
        os.chmod(token, 0o600)
        return token

    def test_owned_gateway_process_lease_starts_from_held_collector_and_reaps(self) -> None:
        from scripts.canary_gateway_egress_topology import (
            _start_gateway_process_lease,
        )

        events: list[object] = []

        class Process:
            pid = 4312

            def wait(self, *, timeout: float) -> int:
                events.append(("wait", timeout))
                return 0

        process = Process()
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.realpath(directory)
            self._prepare_private_root(root)
            environment = self._closed_environment(root)
            collector = os.path.join(root, "openusage-collector")
            config = self._prepare_advise_config(root)
            token = self._prepare_gateway_token(root)
            with open(collector, "wb") as stream:
                stream.write(b"audited-collector")
            os.chmod(collector, 0o700)

            def popen(argv, **kwargs):
                events.append(("popen", tuple(argv), kwargs))
                return process

            def killpg(process_group_id: int, selected_signal: int) -> None:
                events.append(("killpg", process_group_id, selected_signal))

            with (
                patch(
                    "scripts.canary_gateway_egress_topology.subprocess.Popen",
                    side_effect=popen,
                ),
                patch(
                    "scripts.canary_gateway_egress_topology.os.getpgid",
                    return_value=process.pid,
                ),
                patch(
                    "scripts.canary_gateway_egress_topology.os.killpg",
                    side_effect=killpg,
                ),
                patch(
                    "scripts.canary_gateway_egress_topology._wait_for_reserved_leader",
                    return_value=True,
                ),
                patch(
                    "scripts.canary_gateway_egress_topology._process_group_has_live_member",
                    return_value=False,
                ),
            ):
                lease = _start_gateway_process_lease(
                    collector=collector,
                    config_path=config,
                    token_path=token,
                    environment=environment,
                )
                with open(os.path.join(environment["XDG_DATA_HOME"], "activity.sqlite3"), "wb") as stream:
                    stream.write(b"runtime-data")
                lease.stop()
                lease.close()

        popen_event = next(event for event in events if event[0] == "popen")
        _, argv, kwargs = popen_event
        descriptor = int(argv[0].rsplit("/", 1)[1])
        config_descriptor = int(argv[4].rsplit("/", 1)[1])
        self.assertEqual(
            argv,
            (
                f"/proc/self/fd/{descriptor}",
                "gateway",
                "start",
                "--config",
                f"/proc/self/fd/{config_descriptor}",
                "--token-path",
                argv[6],
            ),
        )
        self.assertRegex(
            argv[6],
            r"^/proc/self/fd/[0-9]+$",
        )
        self.assertIn(int(argv[6].rsplit("/", 1)[1]), kwargs["pass_fds"])
        self.assertNotIn(root, argv)
        self.assertEqual(kwargs["pass_fds"][0], descriptor)
        self.assertIn(config_descriptor, kwargs["pass_fds"])
        for key in ("HOME", "XDG_DATA_HOME", "TMPDIR"):
            self.assertRegex(kwargs["env"][key], r"^/proc/self/fd/[0-9]+$")
            self.assertIn(
                int(kwargs["env"][key].rsplit("/", 1)[1]),
                kwargs["pass_fds"],
            )
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["start_new_session"], True)
        self.assertEqual(
            {key: kwargs["env"][key] for key in ("PATH", "LANG", "LC_ALL", "PYTHONNOUSERSITE")},
            {key: environment[key] for key in ("PATH", "LANG", "LC_ALL", "PYTHONNOUSERSITE")},
        )
        self.assertIn(("killpg", process.pid, signal.SIGTERM), events)
        self.assertIn(("killpg", process.pid, signal.SIGKILL), events)
        self.assertIn(("wait", 5.0), events)

    def test_gateway_lease_rejects_root_or_child_directory_authority_drift(self) -> None:
        from scripts.canary_gateway_egress_topology import (
            GatewayEgressTopologyCanaryError,
            _start_gateway_process_lease,
        )

        class Process:
            pid = 4312

            def wait(self, *, timeout: float) -> int:
                del timeout
                return -signal.SIGKILL

        for drift in ("root-entry", "child-rebind", "child-mode"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as directory:
                root = os.path.realpath(directory)
                self._prepare_private_root(root)
                environment = self._closed_environment(root)
                collector = os.path.join(root, "openusage-collector")
                with open(collector, "wb") as stream:
                    stream.write(b"audited-collector")
                os.chmod(collector, 0o700)
                config = self._prepare_advise_config(root)
                token = self._prepare_gateway_token(root)
                with (
                    patch(
                        "scripts.canary_gateway_egress_topology.subprocess.Popen",
                        return_value=Process(),
                    ),
                    patch(
                        "scripts.canary_gateway_egress_topology.os.getpgid",
                        return_value=4312,
                    ),
                    patch(
                        "scripts.canary_gateway_egress_topology.os.killpg",
                    ),
                    patch(
                        "scripts.canary_gateway_egress_topology._wait_for_reserved_leader",
                        return_value=True,
                    ),
                    patch(
                        "scripts.canary_gateway_egress_topology._wait_for_group_members_to_exit",
                    ),
                ):
                    lease = _start_gateway_process_lease(
                        collector=collector,
                        config_path=config,
                        token_path=token,
                        environment=environment,
                    )
                    data = environment["XDG_DATA_HOME"]
                    if drift == "root-entry":
                        with open(os.path.join(root, "foreign.marker"), "wb") as stream:
                            stream.write(b"PRIVATE")
                    elif drift == "child-rebind":
                        os.rename(data, f"{data}.owned-original")
                        os.mkdir(data, 0o700)
                        os.chmod(data, 0o700)
                    else:
                        os.chmod(data, 0o750)
                    with self.assertRaisesRegex(
                        GatewayEgressTopologyCanaryError,
                        r"^Gateway egress topology canary failed$",
                    ):
                        lease.stop()
                    lease.close()

    def test_reaped_gateway_leader_is_never_signalled_after_binding_failure(self) -> None:
        from scripts.canary_gateway_egress_topology import (
            GatewayEgressTopologyCanaryError,
            _start_gateway_process_lease,
        )

        events: list[object] = []

        class Process:
            pid = 4312

            def wait(self, *, timeout: float) -> int:
                events.append(("wait", timeout))
                return 0

        with tempfile.TemporaryDirectory() as directory:
            root = os.path.realpath(directory)
            self._prepare_private_root(root)
            collector = os.path.join(root, "openusage-collector")
            with open(collector, "wb") as stream:
                stream.write(b"audited-collector")
            os.chmod(collector, 0o700)
            config = self._prepare_advise_config(root)
            token = self._prepare_gateway_token(root)

            with (
                patch(
                    "scripts.canary_gateway_egress_topology.subprocess.Popen",
                    return_value=Process(),
                ),
                patch(
                    "scripts.canary_gateway_egress_topology.os.getpgid",
                    return_value=4312,
                ),
                patch(
                    "scripts.canary_gateway_egress_topology.os.killpg",
                    side_effect=lambda pid, selected_signal: events.append(
                        ("killpg", pid, selected_signal)
                    ),
                ),
                patch(
                    "scripts.canary_gateway_egress_topology._wait_for_reserved_leader",
                    return_value=True,
                ),
                patch(
                    "scripts.canary_gateway_egress_topology._process_group_has_live_member",
                    return_value=False,
                ),
            ):
                lease = _start_gateway_process_lease(
                    collector=collector,
                    config_path=config,
                    token_path=token,
                    environment=self._closed_environment(root),
                )
                with patch.object(
                    lease,
                    "_binding_is_stable",
                    side_effect=(True, False),
                ):
                    with self.assertRaisesRegex(
                        GatewayEgressTopologyCanaryError,
                        r"^Gateway egress topology canary failed$",
                    ):
                        lease.stop()
                events_after_stop = list(events)
                lease.close()

        self.assertEqual(events, events_after_stop)
        self.assertEqual(events.count(("wait", 5.0)), 1)
        self.assertEqual(
            [event for event in events if event[0] == "killpg"],
            [
                ("killpg", 4312, signal.SIGTERM),
                ("killpg", 4312, signal.SIGKILL),
            ],
        )

    def test_unverified_gateway_process_is_never_treated_as_an_owned_group(self) -> None:
        from scripts.canary_gateway_egress_topology import (
            GatewayEgressTopologyCanaryError,
            _start_gateway_process_lease,
        )

        events: list[object] = []

        class Process:
            pid = 4312

            def kill(self) -> None:
                events.append("kill-process")

            def wait(self, *, timeout: float) -> int:
                events.append(("wait", timeout))
                return -signal.SIGKILL

        with tempfile.TemporaryDirectory() as directory:
            root = os.path.realpath(directory)
            self._prepare_private_root(root)
            collector = os.path.join(root, "openusage-collector")
            with open(collector, "wb") as stream:
                stream.write(b"audited-collector")
            os.chmod(collector, 0o700)
            config = self._prepare_advise_config(root)
            token = self._prepare_gateway_token(root)

            with (
                patch(
                    "scripts.canary_gateway_egress_topology.subprocess.Popen",
                    return_value=Process(),
                ),
                patch(
                    "scripts.canary_gateway_egress_topology.os.getpgid",
                    return_value=9999,
                ),
                patch(
                    "scripts.canary_gateway_egress_topology.os.killpg",
                    side_effect=lambda *_args: events.append("kill-group"),
                ),
                patch(
                    "scripts.canary_gateway_egress_topology.os.close",
                    wraps=os.close,
                ) as close_descriptor,
            ):
                with self.assertRaisesRegex(
                    GatewayEgressTopologyCanaryError,
                    r"^Gateway egress topology canary failed$",
                ):
                    _start_gateway_process_lease(
                        collector=collector,
                        config_path=config,
                        token_path=token,
                        environment=self._closed_environment(root),
                    )

        self.assertEqual(events, ["kill-process", ("wait", 5.0)])
        self.assertEqual(close_descriptor.call_count, 7)

    def test_gateway_lease_rejects_untrusted_environment_before_popen(self) -> None:
        from scripts.canary_gateway_egress_topology import (
            GatewayEgressTopologyCanaryError,
            _start_gateway_process_lease,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = os.path.realpath(directory)
            self._prepare_private_root(root)
            collector = os.path.join(root, "openusage-collector")
            with open(collector, "wb") as stream:
                stream.write(b"audited-collector")
            os.chmod(collector, 0o700)
            config = self._prepare_advise_config(root)
            token = self._prepare_gateway_token(root)
            environment = self._closed_environment(root) | {
                "LD_PRELOAD": "/PRIVATE/injection.so"
            }
            with (
                patch(
                    "scripts.canary_gateway_egress_topology.subprocess.Popen",
                ) as popen,
                patch(
                    "scripts.canary_gateway_egress_topology.os.close",
                    wraps=os.close,
                ) as close_descriptor,
            ):
                with self.assertRaisesRegex(
                    GatewayEgressTopologyCanaryError,
                    r"^Gateway egress topology canary failed$",
                ):
                    _start_gateway_process_lease(
                        collector=collector,
                        config_path=config,
                        token_path=token,
                        environment=environment,
                    )

        popen.assert_not_called()
        self.assertEqual(close_descriptor.call_count, 0)

    def test_gateway_lease_rejects_symlinked_environment_authority(self) -> None:
        from scripts.canary_gateway_egress_topology import (
            GatewayEgressTopologyCanaryError,
            _start_gateway_process_lease,
        )

        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as foreign_directory,
        ):
            root = os.path.realpath(directory)
            foreign = os.path.realpath(foreign_directory)
            os.chmod(root, 0o700)
            for name in ("data", "tmp"):
                os.mkdir(os.path.join(root, name), 0o700)
            os.symlink(foreign, os.path.join(root, "home"))
            collector = os.path.join(root, "openusage-collector")
            with open(collector, "wb") as stream:
                stream.write(b"audited-collector")
            os.chmod(collector, 0o700)
            config = self._prepare_advise_config(root)
            token = self._prepare_gateway_token(root)
            marker = os.path.join(foreign, "PRIVATE_MARKER")
            with open(marker, "wb") as stream:
                stream.write(b"PRIVATE_BYTES")
            marker_identity = os.lstat(marker)

            with patch(
                "scripts.canary_gateway_egress_topology.subprocess.Popen",
            ) as popen:
                with self.assertRaisesRegex(
                    GatewayEgressTopologyCanaryError,
                    r"^Gateway egress topology canary failed$",
                ):
                    _start_gateway_process_lease(
                        collector=collector,
                        config_path=config,
                        token_path=token,
                        environment=self._closed_environment(root),
                    )

            popen.assert_not_called()
            self.assertEqual(
                (os.lstat(marker).st_dev, os.lstat(marker).st_ino),
                (marker_identity.st_dev, marker_identity.st_ino),
            )
            with open(marker, "rb") as stream:
                self.assertEqual(stream.read(), b"PRIVATE_BYTES")

    def test_gateway_lease_rejects_non_advise_config_before_popen(self) -> None:
        from scripts.canary_gateway_egress_topology import (
            GatewayEgressTopologyCanaryError,
            _start_gateway_process_lease,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = os.path.realpath(directory)
            self._prepare_private_root(root)
            collector = os.path.join(root, "openusage-collector")
            with open(collector, "wb") as stream:
                stream.write(b"audited-collector")
            os.chmod(collector, 0o700)
            config = os.path.join(root, "gateway.json")
            with open(config, "wb") as stream:
                stream.write(
                    b'{"enabled":true,"mode":"gateway",'
                    b'"proxy_enabled":true}'
                )
            os.chmod(config, 0o600)
            token = self._prepare_gateway_token(root)

            with patch(
                "scripts.canary_gateway_egress_topology.subprocess.Popen",
            ) as popen:
                with self.assertRaisesRegex(
                    GatewayEgressTopologyCanaryError,
                    r"^Gateway egress topology canary failed$",
                ):
                    _start_gateway_process_lease(
                        collector=collector,
                        config_path=config,
                        token_path=token,
                        environment=self._closed_environment(root),
                    )

            popen.assert_not_called()

    def test_gateway_lease_revalidates_every_binding_after_popen(self) -> None:
        from scripts.canary_gateway_egress_topology import (
            GatewayEgressTopologyCanaryError,
            _start_gateway_process_lease,
        )

        events: list[object] = []

        class Process:
            pid = 4312

            def wait(self, *, timeout: float) -> int:
                events.append(("wait", timeout))
                return -signal.SIGKILL

        with tempfile.TemporaryDirectory() as directory:
            root = os.path.realpath(directory)
            self._prepare_private_root(root)
            collector = os.path.join(root, "openusage-collector")
            with open(collector, "wb") as stream:
                stream.write(b"audited-collector")
            os.chmod(collector, 0o700)
            config = self._prepare_advise_config(root)
            token = self._prepare_gateway_token(root)

            def popen(_argv, **_kwargs):
                os.rename(config, f"{config}.owned-original")
                with open(config, "wb") as stream:
                    stream.write(b'{"enabled":true,"mode":"advise"}')
                os.chmod(config, 0o600)
                return Process()

            with (
                patch(
                    "scripts.canary_gateway_egress_topology.subprocess.Popen",
                    side_effect=popen,
                ),
                patch(
                    "scripts.canary_gateway_egress_topology.os.getpgid",
                    return_value=4312,
                ),
                patch(
                    "scripts.canary_gateway_egress_topology.os.killpg",
                    side_effect=lambda pid, selected_signal: events.append(
                        ("killpg", pid, selected_signal)
                    ),
                ),
                patch(
                    "scripts.canary_gateway_egress_topology._wait_for_reserved_leader",
                    return_value=True,
                ),
                patch(
                    "scripts.canary_gateway_egress_topology._wait_for_group_members_to_exit",
                ),
            ):
                with self.assertRaisesRegex(
                    GatewayEgressTopologyCanaryError,
                    r"^Gateway egress topology canary failed$",
                ):
                    _start_gateway_process_lease(
                        collector=collector,
                        config_path=config,
                        token_path=token,
                        environment=self._closed_environment(root),
                    )

        self.assertIn(("killpg", 4312, signal.SIGKILL), events)
        self.assertIn(("wait", 5.0), events)

    def test_gateway_lease_reports_unproven_post_spawn_cleanup(self) -> None:
        from scripts.canary_gateway_egress_topology import (
            GatewayEgressTopologyCanaryError,
            _start_gateway_process_lease,
        )

        class Process:
            pid = 4312

        with tempfile.TemporaryDirectory() as directory:
            root = os.path.realpath(directory)
            self._prepare_private_root(root)
            collector = os.path.join(root, "openusage-collector")
            with open(collector, "wb") as stream:
                stream.write(b"audited-collector")
            os.chmod(collector, 0o700)
            config = self._prepare_advise_config(root)
            token = self._prepare_gateway_token(root)

            with (
                patch(
                    "scripts.canary_gateway_egress_topology.subprocess.Popen",
                    return_value=Process(),
                ),
                patch(
                    "scripts.canary_gateway_egress_topology.os.getpgid",
                    return_value=9999,
                ),
                patch(
                    "scripts.canary_gateway_egress_topology._GatewayProcessLease.close",
                    side_effect=RuntimeError("PRIVATE_CLEANUP"),
                ),
            ):
                with self.assertRaisesRegex(
                    GatewayEgressTopologyCanaryError,
                    r"^Gateway egress topology canary failed$",
                ) as raised:
                    _start_gateway_process_lease(
                        collector=collector,
                        config_path=config,
                        token_path=token,
                        environment=self._closed_environment(root),
                    )

        self.assertEqual(raised.exception.stage, "cleanup")
        self.assertNotIn("PRIVATE", str(raised.exception))

    def test_gateway_lease_opens_every_artifact_beneath_the_held_root(self) -> None:
        from scripts.canary_gateway_egress_topology import (
            GatewayEgressTopologyCanaryError,
            _start_gateway_process_lease,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = os.path.realpath(directory)
            self._prepare_private_root(root)
            collector = os.path.join(root, "openusage-collector")
            with open(collector, "wb") as stream:
                stream.write(b"owned-collector")
            os.chmod(collector, 0o700)
            config = self._prepare_advise_config(root)
            token = self._prepare_gateway_token(root)
            original = f"{root}.owned-original"
            real_open = os.open
            swapped = False

            def swap_root_then_open(path, flags, *args, **kwargs):
                nonlocal swapped
                if path == "openusage-collector" and not swapped:
                    swapped = True
                    os.rename(root, original)
                    os.mkdir(root, 0o700)
                    self._prepare_private_root(root)
                    with open(os.path.join(root, "openusage-collector"), "wb") as stream:
                        stream.write(b"foreign-collector")
                    os.chmod(os.path.join(root, "openusage-collector"), 0o700)
                    self._prepare_advise_config(root)
                    self._prepare_gateway_token(root)
                return real_open(path, flags, *args, **kwargs)

            with (
                patch(
                    "scripts.canary_gateway_egress_topology.os.open",
                    side_effect=swap_root_then_open,
                ),
                patch(
                    "scripts.canary_gateway_egress_topology.subprocess.Popen",
                ) as popen,
            ):
                with self.assertRaisesRegex(
                    GatewayEgressTopologyCanaryError,
                    r"^Gateway egress topology canary failed$",
                ):
                    _start_gateway_process_lease(
                        collector=collector,
                        config_path=config,
                        token_path=token,
                        environment=self._closed_environment(root),
                    )

            self.assertTrue(swapped)
            popen.assert_not_called()
            with open(os.path.join(original, "openusage-collector"), "rb") as stream:
                self.assertEqual(stream.read(), b"owned-collector")
            with open(os.path.join(root, "openusage-collector"), "rb") as stream:
                self.assertEqual(stream.read(), b"foreign-collector")

    def test_runner_observes_one_ready_advise_epoch_and_always_stops(self) -> None:
        from openusage_bar.gateway.egress import GatewayEgressAttemptCounters
        from openusage_bar.gateway.server import (
            GatewayAdviseHealthState,
            GatewayFixedUnknownAdviceState,
        )
        from scripts.canary_gateway_egress_topology import (
            GatewayEgressTopologySummary,
            run_gateway_egress_topology_canary,
        )

        events: list[object] = []
        counters = GatewayEgressAttemptCounters("a" * 64, 17, 5)
        health = GatewayAdviseHealthState(
            api_version="gateway.openusage/v1",
            status="ok",
            mode="advise",
            should_send=True,
            responses=False,
        )
        advice = GatewayFixedUnknownAdviceState(
            decision="defer",
            confidence=0.5,
            reason="quota_unknown",
            defer_until=None,
            quota_remaining=None,
            burn_rate_per_minute=None,
            predicted_exhaustion_minutes=None,
        )

        class Lease:
            def leader_has_exited(self) -> bool:
                return False

            def read_token(self) -> str:
                events.append("token")
                return "g" * 48

            def stop(self) -> None:
                events.append("stop")

            def close(self) -> None:
                events.append("close")

        with (
            patch(
                "scripts.canary_gateway_egress_topology._start_gateway_process_lease",
                side_effect=lambda **_kwargs: events.append("start") or Lease(),
            ),
            patch(
                "scripts.canary_gateway_egress_topology.read_gateway_advise_health_state",
                side_effect=lambda **_kwargs: events.append("health") or health,
            ),
            patch(
                "scripts.canary_gateway_egress_topology.read_gateway_egress_attempt_counters",
                side_effect=lambda **_kwargs: events.append("counters") or counters,
            ),
            patch(
                "scripts.canary_gateway_egress_topology.read_gateway_fixed_unknown_advice_state",
                side_effect=lambda **_kwargs: events.append("fixed-advice") or advice,
            ),
            patch(
                "scripts.canary_gateway_egress_topology.evaluate_gateway_egress_attempt_window",
                side_effect=lambda **_kwargs: events.append("evaluate")
                or GatewayEgressTopologySummary(True),
            ),
        ):
            summary = run_gateway_egress_topology_canary(
                collector="/PRIVATE/openusage-collector",
                config_path="/PRIVATE/gateway.json",
                token_path="/PRIVATE/gateway.token",
                port=17823,
                environment=self._closed_environment(),
            )

        self.assertEqual(summary, GatewayEgressTopologySummary(True))
        self.assertEqual(
            events,
            [
                "start",
                "token",
                "health",
                "counters",
                "fixed-advice",
                "health",
                "counters",
                "evaluate",
                "stop",
                "close",
            ],
        )

    def test_runner_rejects_a_hostile_mutated_failure_stage(self) -> None:
        from scripts.canary_gateway_egress_topology import (
            GatewayEgressTopologyCanaryError,
            run_gateway_egress_topology_canary,
        )

        events: list[str] = []

        class HostileStage:
            def __eq__(self, other: object) -> bool:
                del other
                raise RuntimeError("PRIVATE_STAGE_EQ")

        error = GatewayEgressTopologyCanaryError("cleanup")
        error.stage = HostileStage()  # type: ignore[assignment]

        class Lease:
            def read_token(self) -> str:
                raise error

            def close(self) -> None:
                events.append("close")

        with patch(
            "scripts.canary_gateway_egress_topology._start_gateway_process_lease",
            return_value=Lease(),
        ):
            with self.assertRaisesRegex(
                GatewayEgressTopologyCanaryError,
                r"^Gateway egress topology canary failed$",
            ) as raised:
                run_gateway_egress_topology_canary(
                    collector="/PRIVATE/openusage-collector",
                    config_path="/PRIVATE/gateway.json",
                    token_path="/PRIVATE/gateway.token",
                    port=17823,
                    environment=self._closed_environment(),
                )

        self.assertEqual((raised.exception.stage, events), ("token", ["close"]))
        self.assertNotIn("PRIVATE", str(raised.exception))

    def test_runner_rejects_a_mutated_fixed_advice_fact_before_final_counter(self) -> None:
        from openusage_bar.gateway.egress import GatewayEgressAttemptCounters
        from openusage_bar.gateway.server import (
            GatewayAdviseHealthState,
            GatewayFixedUnknownAdviceState,
        )
        from scripts.canary_gateway_egress_topology import (
            GatewayEgressTopologyCanaryError,
            run_gateway_egress_topology_canary,
        )

        events: list[str] = []
        counters = GatewayEgressAttemptCounters("a" * 64, 17, 5)
        advice = GatewayFixedUnknownAdviceState(
            "defer", 0.5, "quota_unknown", None, None, None, None
        )
        equality_calls: list[str] = []

        class EqualToAnything:
            def __eq__(self, _other: object) -> bool:
                equality_calls.append("PRIVATE_EQUALITY")
                return True

        object.__setattr__(advice, "decision", EqualToAnything())
        health = GatewayAdviseHealthState(
            "gateway.openusage/v1", "ok", "advise", True, False
        )

        class Lease:
            def leader_has_exited(self) -> bool:
                return False

            def read_token(self) -> str:
                return "g" * 48

            def stop(self) -> None:
                events.append("stop")

            def close(self) -> None:
                events.append("close")

        with (
            patch(
                "scripts.canary_gateway_egress_topology._start_gateway_process_lease",
                return_value=Lease(),
            ),
            patch(
                "scripts.canary_gateway_egress_topology.read_gateway_advise_health_state",
                return_value=health,
            ),
            patch(
                "scripts.canary_gateway_egress_topology.read_gateway_egress_attempt_counters",
                side_effect=lambda **_kwargs: events.append("counter") or counters,
            ),
            patch(
                "scripts.canary_gateway_egress_topology.read_gateway_fixed_unknown_advice_state",
                return_value=advice,
            ),
        ):
            with self.assertRaisesRegex(
                GatewayEgressTopologyCanaryError,
                r"^Gateway egress topology canary failed$",
            ):
                run_gateway_egress_topology_canary(
                    collector="/PRIVATE/openusage-collector",
                    config_path="/PRIVATE/gateway.json",
                    token_path="/PRIVATE/gateway.token",
                    port=17823,
                    environment=self._closed_environment(),
                )

        self.assertEqual(events, ["counter", "close"])
        self.assertEqual(equality_calls, [])

    def test_readiness_deadline_is_checked_after_successful_health(self) -> None:
        from openusage_bar.gateway.server import GatewayAdviseHealthState
        from scripts.canary_gateway_egress_topology import (
            GatewayEgressTopologyCanaryError,
            _wait_for_advise_health,
        )

        health = GatewayAdviseHealthState(
            "gateway.openusage/v1",
            "ok",
            "advise",
            True,
            False,
        )

        class Lease:
            def leader_has_exited(self) -> bool:
                return False

        clock = iter((100.0, 114.9, 116.0))
        with (
            patch(
                "scripts.canary_gateway_egress_topology.time.monotonic",
                side_effect=lambda: next(clock),
            ),
            patch(
                "scripts.canary_gateway_egress_topology.read_gateway_advise_health_state",
                return_value=health,
            ),
        ):
            with self.assertRaisesRegex(
                GatewayEgressTopologyCanaryError,
                r"^Gateway egress topology canary failed$",
            ):
                _wait_for_advise_health(
                    port=17823,
                    bearer_token="g" * 48,
                    lease=Lease(),
                )

    def test_cli_is_silent_and_accepts_only_one_canonical_private_layout(self) -> None:
        from scripts.canary_gateway_egress_topology import (
            GatewayEgressTopologyCanaryError,
            GatewayEgressTopologySummary,
            main,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = os.path.realpath(directory)
            self._prepare_private_root(root)
            collector = os.path.join(root, "openusage-collector")
            with open(collector, "wb") as stream:
                stream.write(b"audited-collector")
            os.chmod(collector, 0o700)
            self._prepare_advise_config(root)
            self._prepare_gateway_token(root)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch(
                "scripts.canary_gateway_egress_topology.run_gateway_egress_topology_canary",
                return_value=GatewayEgressTopologySummary(True),
            ) as runner:
                code = main(
                    ("--collector", collector, "--port", "17823"),
                    stdout=stdout,
                    stderr=stderr,
                )

            self.assertEqual((code, stdout.getvalue(), stderr.getvalue()), (0, "", ""))
            runner.assert_called_once_with(
                collector=collector,
                config_path=os.path.join(root, "gateway.json"),
                token_path=os.path.join(root, "gateway.token"),
                port=17823,
                environment=self._closed_environment(root),
            )

        for arguments in (
            (),
            ("--collector", "relative", "--port", "17823"),
            ("--collector", "/PRIVATE/collector", "--port", "1"),
            ("--collector", "/PRIVATE/collector", "--unknown", "17823"),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    main(arguments, stdout=io.StringIO(), stderr=io.StringIO()),
                    2,
                )

        class HostileString(str):
            def __eq__(self, other: object) -> bool:
                del other
                raise RuntimeError("PRIVATE_ARGV_SECRET")

            def __ne__(self, other: object) -> bool:
                del other
                raise RuntimeError("PRIVATE_ARGV_SECRET")

        for index in (0, 2, 3):
            arguments = ["--collector", "/PRIVATE/openusage-collector", "--port", "17823"]
            arguments[index] = HostileString(arguments[index])
            with self.subTest(hostile_index=index), patch(
                "scripts.canary_gateway_egress_topology.run_gateway_egress_topology_canary"
            ) as runner:
                stdout = io.StringIO()
                stderr = io.StringIO()
                self.assertEqual(main(arguments, stdout=stdout, stderr=stderr), 2)
                self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
                runner.assert_not_called()

        for stage, expected_code in (
            ("launch", 11),
            ("token", 12),
            ("readiness", 13),
            ("counter-window", 14),
            ("stop", 15),
            ("cleanup", 16),
        ):
            with self.subTest(stage=stage), patch(
                "scripts.canary_gateway_egress_topology.run_gateway_egress_topology_canary",
                side_effect=GatewayEgressTopologyCanaryError(stage),
            ):
                stdout = io.StringIO()
                stderr = io.StringIO()
                self.assertEqual(
                    main(
                        ("--collector", "/PRIVATE/openusage-collector", "--port", "17823"),
                        stdout=stdout,
                        stderr=stderr,
                    ),
                    expected_code,
                )
                self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))

        class HostileStage:
            def __hash__(self) -> int:
                raise RuntimeError("PRIVATE_STAGE_MARKER")

        hostile_error = GatewayEgressTopologyCanaryError("launch")
        hostile_error.stage = HostileStage()  # type: ignore[assignment]
        with patch(
            "scripts.canary_gateway_egress_topology.run_gateway_egress_topology_canary",
            side_effect=hostile_error,
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            self.assertEqual(
                main(
                    ("--collector", "/PRIVATE/openusage-collector", "--port", "17823"),
                    stdout=stdout,
                    stderr=stderr,
                ),
                1,
            )
            self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))

    def test_evaluator_accepts_only_one_authenticated_endpoint_epoch_with_zero_delta(
        self,
    ) -> None:
        from openusage_bar.gateway.egress import GatewayEgressAttemptCounters
        from scripts.canary_gateway_egress_topology import (
            GatewayEgressTopologySummary,
            evaluate_gateway_egress_attempt_window,
        )

        baseline = GatewayEgressAttemptCounters("a" * 64, 17, 5)

        self.assertEqual(
            evaluate_gateway_egress_attempt_window(
                counters_before=baseline,
                counters_after=baseline,
            ),
            GatewayEgressTopologySummary(gateway_egress_attempt_delta_zero=True),
        )

        for name, counters_after in (
            (
                "process_epoch_restart",
                GatewayEgressAttemptCounters("b" * 64, 17, 5),
            ),
            (
                "network_attempt",
                GatewayEgressAttemptCounters("a" * 64, 18, 5),
            ),
            (
                "credential_attempt",
                GatewayEgressAttemptCounters("a" * 64, 17, 6),
            ),
            (
                "counter_regression",
                GatewayEgressAttemptCounters("a" * 64, 16, 5),
            ),
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "Gateway egress topology canary failed",
                ):
                    evaluate_gateway_egress_attempt_window(
                        counters_before=baseline,
                        counters_after=counters_after,
                    )


if __name__ == "__main__":
    unittest.main()
