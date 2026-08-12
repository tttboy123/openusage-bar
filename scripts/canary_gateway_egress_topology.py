#!/usr/bin/env python3
"""Closed evaluator for an authenticated Gateway egress-attempt window.

This is not native lifecycle or release evidence.  It describes only logical
provider-egress attempts exposed by one authenticated endpoint process epoch;
it does not attribute that endpoint to a service or PID and does not cover the
Observer, other processes, DNS, sockets, HTTP success, or credential stores.
"""

from __future__ import annotations

import math
import os
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TextIO

from openusage_bar.gateway.egress import GatewayEgressAttemptCounters
from openusage_bar.gateway.server import (
    GatewayAdviseHealthState,
    read_gateway_advise_health_state,
    read_gateway_egress_attempt_counters,
)


_COLLECTOR_MODE = 0o700
_STOP_SECONDS = 5.0
_READINESS_SECONDS = 15.0
_ADVISE_CONFIG = b'{"enabled":true,"mode":"advise"}'
_ENVIRONMENT_FIXED = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
    "PYTHONNOUSERSITE": "1",
}
_ENVIRONMENT_PATHS = ("HOME", "XDG_DATA_HOME", "TMPDIR")
_ENVIRONMENT_DIRECT_CHILDREN = {
    "HOME": "home",
    "XDG_DATA_HOME": "data",
    "TMPDIR": "tmp",
}
_STAGE_EXIT_CODES = {
    "launch": 11,
    "token": 12,
    "readiness": 13,
    "counter-window": 14,
    "stop": 15,
    "cleanup": 16,
}


class GatewayEgressTopologyCanaryError(RuntimeError):
    """A fixed, value-free diagnostic failure."""

    def __init__(self, stage: str | None = None) -> None:
        if stage is not None and (
            type(stage) is not str or stage not in _STAGE_EXIT_CODES
        ):
            stage = None
        self.stage = stage
        super().__init__("Gateway egress topology canary failed")


@dataclass(frozen=True, repr=False)
class GatewayEgressTopologySummary:
    """Closed result for one authenticated endpoint process epoch."""

    gateway_egress_attempt_delta_zero: bool

    def __post_init__(self) -> None:
        if self.gateway_egress_attempt_delta_zero is not True:
            raise ValueError("Gateway egress topology summary invalid")

    def __repr__(self) -> str:
        return "<GatewayEgressTopologySummary closed>"


def _file_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _directory_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
    )


class _GatewayProcessLease:
    def __init__(
        self,
        *,
        collector: str,
        descriptor: int,
        signature: tuple[int, ...],
        config_descriptor: int,
        config_signature: tuple[int, ...],
        token_descriptor: int,
        token_signature: tuple[int, ...],
        directory_bindings: tuple[tuple[str, int, tuple[int, ...]], ...],
        process: subprocess.Popen[bytes],
    ) -> None:
        self._collector = collector
        self._descriptor = descriptor
        self._signature = signature
        self._config_descriptor = config_descriptor
        self._config_signature = config_signature
        self._token_descriptor = token_descriptor
        self._token_signature = token_signature
        self._directory_bindings = directory_bindings
        self._process = process
        self._owned_process_group = False
        self._stopped = False
        self._leader_reaped = False
        self._closed = False

    def _binding_is_stable(self) -> bool:
        try:
            return (
                _file_signature(os.fstat(self._descriptor)) == self._signature
                and _file_signature(os.lstat(self._collector)) == self._signature
                and _file_signature(os.fstat(self._config_descriptor))
                == self._config_signature
                and _file_signature(os.fstat(self._token_descriptor))
                == self._token_signature
                and all(
                    (
                        _file_signature(os.fstat(descriptor)) == signature
                        and _file_signature(os.lstat(path)) == signature
                        if index == 0
                        else _directory_signature(os.fstat(descriptor)) == signature
                        and _directory_signature(os.lstat(path)) == signature
                    )
                    for index, (path, descriptor, signature) in enumerate(
                        self._directory_bindings
                    )
                )
            )
        except Exception:
            return False

    def read_token(self) -> str:
        try:
            before = os.fstat(self._token_descriptor)
            raw = os.pread(self._token_descriptor, 257, 0)
            after = os.fstat(self._token_descriptor)
            if (
                _file_signature(before) != self._token_signature
                or _file_signature(after) != self._token_signature
            ):
                raise GatewayEgressTopologyCanaryError
            token = raw.decode("ascii", "strict")
            if (
                type(token) is not str
                or not 43 <= len(token) <= 256
                or not token.isascii()
                or any(
                    ord(character) < 0x21 or character.isspace()
                    for character in token
                )
            ):
                raise GatewayEgressTopologyCanaryError
            return token
        except GatewayEgressTopologyCanaryError:
            raise
        except Exception:
            raise GatewayEgressTopologyCanaryError from None

    def leader_has_exited(self) -> bool:
        try:
            status = os.waitid(
                os.P_PID,
                self._process.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except Exception:
            raise GatewayEgressTopologyCanaryError from None
        return status is not None and status.si_pid == self._process.pid

    def stop(self) -> None:
        if (
            not self._owned_process_group
            or self._stopped
            or not self._binding_is_stable()
        ):
            raise GatewayEgressTopologyCanaryError
        process = self._process
        if type(process.pid) is not int or process.pid <= 0:
            raise GatewayEgressTopologyCanaryError
        try:
            if os.getpgid(process.pid) != process.pid:
                raise GatewayEgressTopologyCanaryError
            os.killpg(process.pid, signal.SIGTERM)
            if not _wait_for_reserved_leader(process.pid, _STOP_SECONDS):
                os.killpg(process.pid, signal.SIGKILL)
                if not _wait_for_reserved_leader(process.pid, _STOP_SECONDS):
                    raise GatewayEgressTopologyCanaryError
            else:
                os.killpg(process.pid, signal.SIGKILL)
            _wait_for_group_members_to_exit(process.pid)
            result = process.wait(timeout=_STOP_SECONDS)
            if type(result) is not int:
                raise GatewayEgressTopologyCanaryError
            self._leader_reaped = True
            if not self._binding_is_stable():
                raise GatewayEgressTopologyCanaryError
            self._stopped = True
        except GatewayEgressTopologyCanaryError:
            raise
        except Exception:
            raise GatewayEgressTopologyCanaryError from None

    def close(self) -> None:
        if self._closed:
            return
        failed = False
        if not self._stopped and not self._leader_reaped:
            process_id = self._process.pid
            if type(process_id) is not int or process_id <= 0:
                failed = True
            else:
                if self._owned_process_group:
                    try:
                        os.killpg(process_id, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except Exception:
                        failed = True
                else:
                    try:
                        self._process.kill()
                    except ProcessLookupError:
                        pass
                    except Exception:
                        failed = True
                try:
                    if self._owned_process_group:
                        _wait_for_reserved_leader(process_id, _STOP_SECONDS)
                except Exception:
                    failed = True
                try:
                    if self._owned_process_group:
                        _wait_for_group_members_to_exit(process_id)
                except Exception:
                    failed = True
                try:
                    result = self._process.wait(timeout=_STOP_SECONDS)
                    if type(result) is not int:
                        failed = True
                    else:
                        self._leader_reaped = True
                except Exception:
                    failed = True
        try:
            os.close(self._descriptor)
        except Exception:
            failed = True
        try:
            os.close(self._config_descriptor)
        except Exception:
            failed = True
        try:
            os.close(self._token_descriptor)
        except Exception:
            failed = True
        for _path, descriptor, _signature in reversed(self._directory_bindings):
            try:
                os.close(descriptor)
            except Exception:
                failed = True
        self._closed = True
        if failed:
            raise GatewayEgressTopologyCanaryError


def _start_gateway_process_lease(
    *,
    collector: str,
    config_path: str,
    token_path: str,
    environment: dict[str, str],
) -> _GatewayProcessLease:
    descriptor: int | None = None
    config_descriptor: int | None = None
    token_descriptor: int | None = None
    directory_bindings: list[tuple[str, int, tuple[int, ...]]] = []
    lease: _GatewayProcessLease | None = None
    try:
        expected_environment_keys = set(_ENVIRONMENT_FIXED) | set(
            _ENVIRONMENT_PATHS
        )
        root = os.path.dirname(collector)
        if (
            type(collector) is not str
            or not os.path.isabs(collector)
            or type(config_path) is not str
            or not os.path.isabs(config_path)
            or type(token_path) is not str
            or not os.path.isabs(token_path)
            or type(environment) is not dict
            or set(environment) != expected_environment_keys
            or any(
                type(key) is not str or type(value) is not str
                for key, value in environment.items()
            )
            or any(
                environment[key] != expected
                for key, expected in _ENVIRONMENT_FIXED.items()
            )
            or any(
                not os.path.isabs(environment[key])
                for key in _ENVIRONMENT_PATHS
            )
            or any(
                environment[key]
                != os.path.join(root, _ENVIRONMENT_DIRECT_CHILDREN[key])
                for key in _ENVIRONMENT_PATHS
            )
        ):
            raise GatewayEgressTopologyCanaryError
        if (
            config_path != os.path.join(root, "gateway.json")
            or token_path != os.path.join(root, "gateway.token")
        ):
            raise GatewayEgressTopologyCanaryError
        directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        root_descriptor = os.open(root, directory_flags)
        root_metadata = os.fstat(root_descriptor)
        root_signature = _file_signature(root_metadata)
        directory_bindings.append((root, root_descriptor, root_signature))
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.getuid()
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
            or _file_signature(os.lstat(root)) != root_signature
        ):
            raise GatewayEgressTopologyCanaryError
        for key in _ENVIRONMENT_PATHS:
            child_name = _ENVIRONMENT_DIRECT_CHILDREN[key]
            child_path = environment[key]
            child_descriptor = os.open(
                child_name,
                directory_flags,
                dir_fd=root_descriptor,
            )
            child_metadata = os.fstat(child_descriptor)
            child_signature = _directory_signature(child_metadata)
            directory_bindings.append(
                (child_path, child_descriptor, child_signature)
            )
            if (
                not stat.S_ISDIR(child_metadata.st_mode)
                or child_metadata.st_uid != os.getuid()
                or stat.S_IMODE(child_metadata.st_mode) != 0o700
                or _directory_signature(os.lstat(child_path)) != child_signature
            ):
                raise GatewayEgressTopologyCanaryError
        descriptor = os.open(
            "openusage-collector",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_descriptor,
        )
        metadata = os.fstat(descriptor)
        signature = _file_signature(metadata)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or stat.S_IMODE(metadata.st_mode) != _COLLECTOR_MODE
            or _file_signature(os.lstat(collector)) != signature
        ):
            raise GatewayEgressTopologyCanaryError
        config_descriptor = os.open(
            "gateway.json",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_descriptor,
        )
        config_metadata = os.fstat(config_descriptor)
        config_signature = _file_signature(config_metadata)
        if (
            not stat.S_ISREG(config_metadata.st_mode)
            or config_metadata.st_uid != os.getuid()
            or config_metadata.st_nlink != 1
            or not 1 <= config_metadata.st_size <= 64 * 1024
            or stat.S_IMODE(config_metadata.st_mode) != 0o600
            or _file_signature(os.lstat(config_path)) != config_signature
        ):
            raise GatewayEgressTopologyCanaryError
        try:
            config_bytes = os.pread(
                config_descriptor,
                len(_ADVISE_CONFIG) + 1,
                0,
            )
        except Exception:
            raise GatewayEgressTopologyCanaryError from None
        if (
            config_bytes != _ADVISE_CONFIG
            or _file_signature(os.fstat(config_descriptor)) != config_signature
            or _file_signature(os.lstat(config_path)) != config_signature
        ):
            raise GatewayEgressTopologyCanaryError
        token_descriptor = os.open(
            "gateway.token",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_descriptor,
        )
        token_metadata = os.fstat(token_descriptor)
        token_signature = _file_signature(token_metadata)
        if (
            not stat.S_ISREG(token_metadata.st_mode)
            or token_metadata.st_uid != os.getuid()
            or token_metadata.st_nlink != 1
            or not 43 <= token_metadata.st_size <= 256
            or stat.S_IMODE(token_metadata.st_mode) != 0o600
            or _file_signature(os.lstat(token_path)) != token_signature
        ):
            raise GatewayEgressTopologyCanaryError
        alias = f"/proc/self/fd/{descriptor}"
        config_alias = f"/proc/self/fd/{config_descriptor}"
        token_alias = f"/proc/self/fd/{token_descriptor}"
        child_environment = {
            **_ENVIRONMENT_FIXED,
            **{
                key: f"/proc/self/fd/{directory_bindings[index + 1][1]}"
                for index, key in enumerate(_ENVIRONMENT_PATHS)
            },
        }
        passed_descriptors = (
            descriptor,
            config_descriptor,
            token_descriptor,
            *(binding[1] for binding in directory_bindings[1:]),
        )
        process = subprocess.Popen(
            (
                alias,
                "gateway",
                "start",
                "--config",
                config_alias,
                "--token-path",
                token_alias,
            ),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=child_environment,
            pass_fds=passed_descriptors,
        )
        lease = _GatewayProcessLease(
            collector=collector,
            descriptor=descriptor,
            signature=signature,
            config_descriptor=config_descriptor,
            config_signature=config_signature,
            token_descriptor=token_descriptor,
            token_signature=token_signature,
            directory_bindings=tuple(directory_bindings),
            process=process,
        )
        if (
            type(process.pid) is not int
            or process.pid <= 0
            or os.getpgid(process.pid) != process.pid
        ):
            raise GatewayEgressTopologyCanaryError
        lease._owned_process_group = True
        if not lease._binding_is_stable():
            raise GatewayEgressTopologyCanaryError
        return lease
    except Exception as error:
        cleanup_failed = False
        if lease is not None:
            try:
                lease.close()
            except Exception:
                cleanup_failed = True
        elif descriptor is not None:
            try:
                os.close(descriptor)
            except Exception:
                cleanup_failed = True
        if lease is None and config_descriptor is not None:
            try:
                os.close(config_descriptor)
            except Exception:
                cleanup_failed = True
        if lease is None and token_descriptor is not None:
            try:
                os.close(token_descriptor)
            except Exception:
                cleanup_failed = True
        if lease is None:
            for _path, directory_descriptor, _signature in reversed(
                directory_bindings
            ):
                try:
                    os.close(directory_descriptor)
                except Exception:
                    cleanup_failed = True
        if cleanup_failed:
            raise GatewayEgressTopologyCanaryError("cleanup") from None
        if type(error) is GatewayEgressTopologyCanaryError:
            raise error from None
        raise GatewayEgressTopologyCanaryError from None


def _wait_for_reserved_leader(process_id: int, timeout: float) -> bool:
    started = time.monotonic()
    if (
        type(started) not in {int, float}
        or not math.isfinite(started)
        or started < 0
    ):
        raise GatewayEgressTopologyCanaryError
    deadline = float(started) + timeout
    if not math.isfinite(deadline):
        raise GatewayEgressTopologyCanaryError
    last = float(started)
    while True:
        try:
            status = os.waitid(
                os.P_PID,
                process_id,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except Exception:
            raise GatewayEgressTopologyCanaryError from None
        if status is not None and status.si_pid == process_id:
            return True
        current = time.monotonic()
        if (
            type(current) not in {int, float}
            or not math.isfinite(current)
            or current < last
            or current >= deadline
        ):
            return False
        last = float(current)
        time.sleep(min(0.05, deadline - last))


def _process_group_has_live_member(process_group_id: int) -> bool:
    try:
        entries = os.listdir("/proc")
    except Exception:
        raise GatewayEgressTopologyCanaryError from None
    for entry in entries:
        if not entry.isascii() or not entry.isdecimal():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as process_stat:
                raw = process_stat.read(4097)
            if len(raw) > 4096:
                raise GatewayEgressTopologyCanaryError
            closing = raw.rfind(b")")
            fields = raw[closing + 2 :].split()
            if closing <= 0 or len(fields) < 3:
                raise GatewayEgressTopologyCanaryError
            state = fields[0]
            group = int(fields[2])
        except (FileNotFoundError, ProcessLookupError):
            continue
        except Exception:
            raise GatewayEgressTopologyCanaryError from None
        if group == process_group_id and state != b"Z":
            return True
    return False


def _wait_for_group_members_to_exit(process_group_id: int) -> None:
    started = time.monotonic()
    if (
        type(started) not in {int, float}
        or not math.isfinite(started)
        or started < 0
    ):
        raise GatewayEgressTopologyCanaryError
    deadline = float(started) + _STOP_SECONDS
    if not math.isfinite(deadline):
        raise GatewayEgressTopologyCanaryError
    last = float(started)
    while _process_group_has_live_member(process_group_id):
        current = time.monotonic()
        if (
            type(current) not in {int, float}
            or not math.isfinite(current)
            or current < last
            or current >= deadline
        ):
            raise GatewayEgressTopologyCanaryError
        last = float(current)
        time.sleep(min(0.05, deadline - last))


def _wait_for_advise_health(
    *,
    port: int,
    bearer_token: str,
    lease: _GatewayProcessLease,
) -> GatewayAdviseHealthState:
    started = time.monotonic()
    if (
        type(started) not in {int, float}
        or not math.isfinite(started)
        or started < 0
    ):
        raise GatewayEgressTopologyCanaryError
    deadline = float(started) + _READINESS_SECONDS
    if not math.isfinite(deadline):
        raise GatewayEgressTopologyCanaryError
    last = float(started)

    def check_clock() -> float:
        nonlocal last
        current = time.monotonic()
        if (
            type(current) not in {int, float}
            or not math.isfinite(current)
            or current < last
            or current >= deadline
        ):
            raise GatewayEgressTopologyCanaryError
        last = float(current)
        return last

    while True:
        try:
            check_clock()
            observed = read_gateway_advise_health_state(
                port=port,
                bearer_token=bearer_token,
            )
            if type(observed) is not GatewayAdviseHealthState:
                raise GatewayEgressTopologyCanaryError
            check_clock()
            return observed
        except GatewayEgressTopologyCanaryError:
            raise
        except Exception:
            if lease.leader_has_exited():
                raise GatewayEgressTopologyCanaryError from None
            try:
                current = check_clock()
            except GatewayEgressTopologyCanaryError:
                raise GatewayEgressTopologyCanaryError from None
            time.sleep(min(0.1, deadline - last))


def run_gateway_egress_topology_canary(
    *,
    collector: str,
    config_path: str,
    token_path: str,
    port: int,
    environment: dict[str, str],
) -> GatewayEgressTopologySummary:
    """Observe one ready advise endpoint epoch and always reclaim its lease."""

    if type(port) is not int or not 1 <= port <= 65535:
        raise GatewayEgressTopologyCanaryError
    lease: _GatewayProcessLease | None = None
    result: GatewayEgressTopologySummary | None = None
    failed = False
    failure_stage: str | None = None
    stage = "launch"
    try:
        lease = _start_gateway_process_lease(
            collector=collector,
            config_path=config_path,
            token_path=token_path,
            environment=environment,
        )
        stage = "token"
        token = lease.read_token()
        stage = "readiness"
        _wait_for_advise_health(port=port, bearer_token=token, lease=lease)
        stage = "counter-window"
        counters_before = read_gateway_egress_attempt_counters(
            port=port,
            bearer_token=token,
        )
        health = read_gateway_advise_health_state(
            port=port,
            bearer_token=token,
        )
        if type(health) is not GatewayAdviseHealthState:
            raise GatewayEgressTopologyCanaryError
        counters_after = read_gateway_egress_attempt_counters(
            port=port,
            bearer_token=token,
        )
        result = evaluate_gateway_egress_attempt_window(
            counters_before=counters_before,
            counters_after=counters_after,
        )
        stage = "stop"
        lease.stop()
    except Exception as error:
        failed = True
        if (
            type(error) is GatewayEgressTopologyCanaryError
            and type(error.stage) is str
            and error.stage == "cleanup"
        ):
            failure_stage = "cleanup"
        else:
            failure_stage = stage
    finally:
        if lease is not None:
            try:
                lease.close()
            except Exception:
                failed = True
                failure_stage = "cleanup"
    if (
        failed
        or type(result) is not GatewayEgressTopologySummary
    ):
        raise GatewayEgressTopologyCanaryError(failure_stage) from None
    return result


def evaluate_gateway_egress_attempt_window(
    *,
    counters_before: GatewayEgressAttemptCounters,
    counters_after: GatewayEgressAttemptCounters,
) -> GatewayEgressTopologySummary:
    """Accept only one unchanged authenticated endpoint epoch snapshot."""

    if (
        type(counters_before) is not GatewayEgressAttemptCounters
        or type(counters_after) is not GatewayEgressAttemptCounters
        or counters_after != counters_before
    ):
        raise GatewayEgressTopologyCanaryError
    return GatewayEgressTopologySummary(gateway_egress_attempt_delta_zero=True)


def main(
    arguments: tuple[str, ...] | list[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Run the silent hosted empty-window diagnostic."""

    del stdout, stderr
    try:
        selected = tuple(sys.argv[1:] if arguments is None else arguments)
        valid = (
            len(selected) == 4
            and all(type(item) is str for item in selected)
            and selected[0] == "--collector"
            and os.path.isabs(selected[1])
            and "\0" not in selected[1]
            and selected[2:] == ("--port", "17823")
        )
    except Exception:
        return 2
    if not valid:
        return 2
    collector = selected[1]
    root = os.path.dirname(collector)
    if os.path.basename(collector) != "openusage-collector":
        return 2
    environment = {
        **_ENVIRONMENT_FIXED,
        **{
            key: os.path.join(root, child)
            for key, child in _ENVIRONMENT_DIRECT_CHILDREN.items()
        },
    }
    try:
        summary = run_gateway_egress_topology_canary(
            collector=collector,
            config_path=os.path.join(root, "gateway.json"),
            token_path=os.path.join(root, "gateway.token"),
            port=17823,
            environment=environment,
        )
        if (
            type(summary) is not GatewayEgressTopologySummary
            or summary.gateway_egress_attempt_delta_zero is not True
        ):
            raise GatewayEgressTopologyCanaryError
    except GatewayEgressTopologyCanaryError as error:
        if type(error.stage) is str:
            return _STAGE_EXIT_CODES.get(error.stage, 1)
        return 1
    except Exception:
        return 1
    return 0


__all__ = [
    "GatewayEgressTopologyCanaryError",
    "GatewayEgressTopologySummary",
    "evaluate_gateway_egress_attempt_window",
    "run_gateway_egress_topology_canary",
]


if __name__ == "__main__":
    raise SystemExit(main())
