from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import BinaryIO, Mapping, Sequence


_READ_CHUNK_BYTES = 16 * 1024


class BoundedProcessError(RuntimeError):
    """A fixed diagnostic that never contains argv or captured output."""

    def __init__(self, code: str) -> None:
        if code not in {"timeout", "output_overflow", "reader_failed"}:
            code = "runner_failed"
        self.code = code
        super().__init__(f"bounded process failed: {code}")


def _read_stream(
    stream: BinaryIO,
    target: bytearray,
    limit: int,
    overflow: threading.Event,
    reader_failed: threading.Event,
) -> None:
    try:
        while True:
            chunk = os.read(stream.fileno(), _READ_CHUNK_BYTES)
            if not chunk:
                return
            remaining = limit - len(target)
            if len(chunk) > remaining:
                if remaining > 0:
                    target.extend(chunk[:remaining])
                overflow.set()
                return
            target.extend(chunk)
    except Exception:
        reader_failed.set()


def run_bounded(
    args: Sequence[str],
    *,
    timeout: float,
    stdout_limit: int = 1024 * 1024,
    stderr_limit: int = 64 * 1024,
    shell: bool = False,
    stdin: int | None = subprocess.DEVNULL,
    stdout: int | None = subprocess.PIPE,
    stderr: int | None = subprocess.PIPE,
    check: bool = False,
    text: bool = False,
    encoding: str | None = None,
    errors: str | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
    """Run a one-shot argv in its own process group with bounded time/output.

    The child is always launched in binary mode.  Decoding happens only after
    the complete process group has been reaped, which keeps the stream limits
    byte-accurate and makes the result compatible with ``subprocess.run``.
    All production callers use this as a one-shot runner; descendants that
    deliberately leave the spawned session are outside its cleanup boundary.
    """
    if shell or not args or timeout <= 0:
        raise ValueError("invalid bounded process request")
    if stdout not in {subprocess.PIPE, subprocess.DEVNULL}:
        raise ValueError("stdout must be PIPE or DEVNULL")
    if stderr not in {subprocess.PIPE, subprocess.DEVNULL}:
        raise ValueError("stderr must be PIPE or DEVNULL")
    if stdout_limit < 0 or stderr_limit < 0:
        raise ValueError("stream limits must be nonnegative")

    process = subprocess.Popen(
        list(args),
        shell=False,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        env=None if env is None else dict(env),
        start_new_session=True,
    )
    # start_new_session makes the child the leader of a new process group. Keep
    # that stable identifier now: the direct child may exit while descendants
    # continue holding a captured pipe open.
    process_group_id = process.pid
    captured_stdout = bytearray()
    captured_stderr = bytearray()
    overflow = threading.Event()
    reader_failed = threading.Event()
    readers: list[threading.Thread] = []
    for stream, target, limit in (
        (process.stdout, captured_stdout, stdout_limit),
        (process.stderr, captured_stderr, stderr_limit),
    ):
        if stream is not None:
            reader = threading.Thread(
                target=_read_stream,
                args=(stream, target, limit, overflow, reader_failed),
                name="bounded-process-reader",
                daemon=True,
            )
            readers.append(reader)
            reader.start()

    def terminate_group() -> None:
        if process_group_id != os.getpgrp():
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        if process.poll() is None:
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass

    def direct_child_exited() -> bool:
        # WNOWAIT keeps the exited leader reserved until group cleanup, avoiding
        # a PID/PGID reuse race between observing success and calling killpg.
        status = os.waitid(
            os.P_PID,
            process.pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
        return status is not None and status.si_pid == process.pid

    deadline = time.monotonic() + timeout
    failure_code: str | None = None
    try:
        while True:
            if overflow.is_set():
                failure_code = "output_overflow"
                break
            if reader_failed.is_set():
                failure_code = "reader_failed"
                break
            process_exited = direct_child_exited()
            readers_exited = all(not reader.is_alive() for reader in readers)
            if process_exited and readers_exited:
                break
            if time.monotonic() >= deadline:
                failure_code = "timeout"
                break
            time.sleep(0.01)
        # A successful one-shot may still have descendants that closed or
        # redirected inherited pipes. On every exit, clean the original group;
        # a descendant that deliberately created a new session is out of scope.
        terminate_group()
    finally:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            terminate_group()
            process.wait()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        for reader in readers:
            reader.join(timeout=1)

    if failure_code is not None:
        raise BoundedProcessError(failure_code)

    stdout_value: bytes | str = bytes(captured_stdout)
    stderr_value: bytes | str = bytes(captured_stderr)
    if text or encoding is not None:
        codec = encoding or "utf-8"
        decode_errors = errors or "strict"
        stdout_value = stdout_value.decode(codec, decode_errors)
        stderr_value = stderr_value.decode(codec, decode_errors)
    completed = subprocess.CompletedProcess(
        list(args), process.returncode, stdout_value, stderr_value
    )
    if check:
        completed.check_returncode()
    return completed
