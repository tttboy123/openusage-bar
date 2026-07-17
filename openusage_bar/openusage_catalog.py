from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import BinaryIO, Callable

from .openusage_adapter import child_subprocess_environment, openusage_path
from .provider_catalog import catalog


EXPECTED_PROVIDER_IDS = tuple(sorted(catalog.upstream_family_ids))
_PROVIDER_ROW = re.compile(rb"^  - ([A-Za-z0-9._-]+)\r?\n?$")
_VERSION_LINE = re.compile(
    rb"^([0-9]+\.[0-9]+\.[0-9]+) \(([0-9a-f]{7,40})\) built [^\r\n]+\r?\n?$"
)
_MAX_STREAM_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 4096
_OUTCOMES = frozenset(
    {
        "ok",
        "openusage_unavailable",
        "unsupported_openusage_version",
        "provider_catalog_drift",
        "invalid_detect_output",
        "timeout",
    }
)


@dataclass(frozen=True)
class CatalogDiagnostic:
    outcome: str
    expected_count: int
    actual_count: int
    missing_count: int
    extra_count: int
    checked_at: datetime

    def __post_init__(self) -> None:
        if self.outcome not in _OUTCOMES:
            raise ValueError("invalid catalog diagnostic outcome")
        for value in (
            self.expected_count,
            self.actual_count,
            self.missing_count,
            self.extra_count,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("invalid catalog diagnostic count")
        if self.checked_at.tzinfo is None or self.checked_at.utcoffset() is None:
            raise ValueError("catalog diagnostic time must include a timezone")

    @property
    def error_code(self) -> str | None:
        if self.outcome == "ok":
            return None
        return (
            f"{self.outcome}_e{self.expected_count}_a{self.actual_count}"
            f"_m{self.missing_count}_x{self.extra_count}"
        )


@dataclass(frozen=True)
class _CommandResult:
    outcome: str
    stdout: bytes = b""


def parse_registered_providers(output: str | bytes) -> tuple[str, ...]:
    """Parse only the exact provider-list section from bounded detect output."""
    raw = output.encode("utf-8", "strict") if isinstance(output, str) else output
    if not isinstance(raw, bytes) or len(raw) > _MAX_STREAM_BYTES:
        raise ValueError("invalid detect output")
    lines = raw.splitlines(keepends=True)
    try:
        header = next(
            index
            for index, line in enumerate(lines)
            if line.rstrip(b"\r\n") == b"All registered providers:"
        )
    except StopIteration as error:
        raise ValueError("invalid detect output") from error
    providers: list[str] = []
    for line in lines[header + 1 :]:
        if not line.strip():
            if providers:
                break
            continue
        match = _PROVIDER_ROW.fullmatch(line)
        if match is None:
            # A non-indented heading terminates the section. Any indented row is
            # rejected so credential-like material can never become catalog data.
            if not line.startswith((b" ", b"\t")) and providers:
                break
            raise ValueError("invalid detect output")
        try:
            provider_id = match.group(1).decode("ascii")
        except UnicodeError as error:
            raise ValueError("invalid detect output") from error
        if provider_id in providers:
            raise ValueError("invalid detect output")
        providers.append(provider_id)
    if not providers:
        raise ValueError("invalid detect output")
    return tuple(sorted(providers))


class OpenUsageCatalogDiscovery:
    """Credential-free, bounded compatibility check for the OpenUsage binary."""

    def __init__(
        self,
        *,
        openusage_path: str | None = None,
        timeout_seconds: float = 5,
        environment: dict[str, str] | None = None,
        path_exists: Callable[[str], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
        popen: Callable[..., subprocess.Popen[bytes]] | None = None,
    ) -> None:
        self.openusage_path = openusage_path or globals()["openusage_path"]()
        self.timeout_seconds = timeout_seconds
        self.environment = environment
        self.path_exists = path_exists
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.popen = popen or subprocess.Popen

    @staticmethod
    def _read_bounded(
        stream: BinaryIO,
        output: bytearray,
        overflow: threading.Event,
    ) -> None:
        try:
            while True:
                chunk = stream.read(_READ_CHUNK_BYTES)
                if not chunk:
                    return
                remaining = _MAX_STREAM_BYTES - len(output)
                if len(chunk) > remaining:
                    if remaining > 0:
                        output.extend(chunk[:remaining])
                    overflow.set()
                    return
                output.extend(chunk)
        except Exception:
            overflow.set()

    def _command(self, arguments: list[str]) -> _CommandResult:
        stdout = bytearray()
        stderr = bytearray()
        overflow = threading.Event()
        try:
            process = self.popen(
                [self.openusage_path, *arguments],
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=child_subprocess_environment(self.environment, self.path_exists),
                start_new_session=True,
            )
        except OSError:
            return _CommandResult("openusage_unavailable")
        except Exception:
            return _CommandResult("invalid_detect_output")
        assert process.stdout is not None and process.stderr is not None
        process_group_id: int | None = None
        try:
            observed_group = os.getpgid(process.pid)
            if observed_group == process.pid and observed_group != os.getpgrp():
                process_group_id = observed_group
        except (OSError, ProcessLookupError):
            pass
        readers = (
            threading.Thread(
                target=self._read_bounded,
                args=(process.stdout, stdout, overflow),
                daemon=True,
            ),
            threading.Thread(
                target=self._read_bounded,
                args=(process.stderr, stderr, overflow),
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + self.timeout_seconds
        timed_out = False
        must_terminate = False

        def kill_owned_process_group() -> None:
            if process_group_id is not None and process_group_id != os.getpgrp():
                try:
                    os.killpg(process_group_id, signal.SIGKILL)
                    return
                except ProcessLookupError:
                    return
                except OSError:
                    pass
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                except OSError:
                    pass

        try:
            while True:
                process_exited = process.poll() is not None
                readers_exited = all(not reader.is_alive() for reader in readers)
                if process_exited and readers_exited:
                    break
                if overflow.is_set():
                    must_terminate = True
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    must_terminate = True
                    break
                time.sleep(min(0.01, max(0.001, deadline - time.monotonic())))
            if must_terminate:
                kill_owned_process_group()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                kill_owned_process_group()
                process.wait(timeout=0.5)
        except Exception:
            kill_owned_process_group()
            try:
                process.wait(timeout=0.5)
            except Exception:
                pass
            return _CommandResult("invalid_detect_output")
        finally:
            reader_deadline = time.monotonic() + 0.5
            for reader in readers:
                reader.join(max(0, reader_deadline - time.monotonic()))
            # Closing a pipe while another thread owns a blocking read can itself
            # block. A validated group kill closes every inherited writer; only
            # close readers that reached EOF within the bounded join.
            if all(not reader.is_alive() for reader in readers):
                try:
                    process.stdout.close()
                    process.stderr.close()
                except Exception:
                    pass
        if timed_out:
            return _CommandResult("timeout")
        if (
            overflow.is_set()
            or any(reader.is_alive() for reader in readers)
            or process.returncode != 0
        ):
            return _CommandResult("invalid_detect_output")
        return _CommandResult("ok", bytes(stdout))

    def _diagnostic(
        self,
        outcome: str,
        *,
        actual_count: int = 0,
        missing_count: int = 0,
        extra_count: int = 0,
    ) -> CatalogDiagnostic:
        return CatalogDiagnostic(
            outcome,
            len(EXPECTED_PROVIDER_IDS),
            actual_count,
            missing_count,
            extra_count,
            self.clock().astimezone(timezone.utc),
        )

    def run(self) -> CatalogDiagnostic:
        version = self._command(["version"])
        if version.outcome != "ok":
            return self._diagnostic(version.outcome)
        match = _VERSION_LINE.fullmatch(version.stdout)
        if match is None:
            return self._diagnostic("unsupported_openusage_version")
        if (
            match.group(1).decode("ascii") != catalog.upstream_version
            or match.group(2).decode("ascii") != catalog.upstream_revision
        ):
            return self._diagnostic("unsupported_openusage_version")
        detected = self._command(["detect", "--all"])
        if detected.outcome != "ok":
            return self._diagnostic(detected.outcome)
        try:
            actual = set(parse_registered_providers(detected.stdout))
        except (TypeError, ValueError, UnicodeError):
            return self._diagnostic("invalid_detect_output")
        expected = set(EXPECTED_PROVIDER_IDS)
        missing_count = len(expected - actual)
        extra_count = len(actual - expected)
        outcome = "ok" if not missing_count and not extra_count else "provider_catalog_drift"
        return self._diagnostic(
            outcome,
            actual_count=len(actual),
            missing_count=missing_count,
            extra_count=extra_count,
        )
