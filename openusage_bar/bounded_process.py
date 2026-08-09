from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, BinaryIO, Callable, Mapping, Sequence


_READ_CHUNK_BYTES = 16 * 1024
_WINDOWS_CREATE_NEW_PROCESS_GROUP = getattr(
    subprocess,
    "CREATE_NEW_PROCESS_GROUP",
    0x00000200,
)


class BoundedProcessError(RuntimeError):
    """A fixed diagnostic that never contains argv or captured output."""

    def __init__(self, code: str) -> None:
        if code not in {
            "timeout",
            "output_overflow",
            "reader_failed",
            "runner_failed",
        }:
            code = "runner_failed"
        self.code = code
        super().__init__(f"bounded process failed: {code}")


def _runner_failed() -> BoundedProcessError:
    return BoundedProcessError("runner_failed")


class _PosixProcessScope:
    popen_kwargs = {"start_new_session": True}

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._process_group_id: int | None = None

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        # start_new_session makes the child the leader of a new process group.
        # Preserve the identifier before the direct child can exit.
        self._process_group_id = process.pid

    def direct_child_exited(self) -> bool:
        process = self._process
        if process is None:
            raise _runner_failed()
        # WNOWAIT keeps the exited leader reserved until group cleanup, avoiding
        # a PID/PGID reuse race between observing success and calling killpg.
        status = os.waitid(
            os.P_PID,
            process.pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
        return status is not None and status.si_pid == process.pid

    def terminate(self) -> None:
        process = self._process
        process_group_id = self._process_group_id
        if process is None or process_group_id is None:
            raise _runner_failed()
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

    def close(self) -> None:
        return


class _WindowsProcessScope:
    popen_kwargs = {"creationflags": _WINDOWS_CREATE_NEW_PROCESS_GROUP}

    def __init__(self, job: Any) -> None:
        self._job = job
        self._process: subprocess.Popen[bytes] | None = None

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        try:
            self._job.assign(process)
        except Exception:
            raise _runner_failed() from None
        self._process = process

    def direct_child_exited(self) -> bool:
        if self._process is None:
            raise _runner_failed()
        try:
            return self._process.poll() is not None
        except Exception:
            raise _runner_failed() from None

    def terminate(self) -> None:
        try:
            self._job.terminate()
        except Exception:
            raise _runner_failed() from None

    def close(self) -> None:
        try:
            self._job.close()
        except Exception:
            raise _runner_failed() from None


def _make_process_scope(
    platform: str,
    *,
    windows_job_factory: Callable[[], Any] | None = None,
) -> _PosixProcessScope | _WindowsProcessScope:
    if platform == "win32":
        if windows_job_factory is None:
            from ._windows_job import create_windows_job

            windows_job_factory = create_windows_job
        try:
            return _WindowsProcessScope(windows_job_factory())
        except Exception:
            raise _runner_failed() from None
    return _PosixProcessScope()


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
    input_data: bytes | None = None,
    _platform: str | None = None,
    _scope_factory: Callable[[str], Any] | None = None,
) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
    """Run a one-shot argv in a platform-native bounded process scope.

    The child is always launched in binary mode.  Decoding happens only after
    the complete process scope has been cleaned, which keeps the stream limits
    byte-accurate and makes the result compatible with ``subprocess.run``.
    POSIX callers retain session/process-group cleanup; Windows callers use a
    kill-on-close Job Object.  All diagnostics exclude argv and child output.
    """
    if shell or not args or timeout <= 0:
        raise ValueError("invalid bounded process request")
    if stdout not in {subprocess.PIPE, subprocess.DEVNULL}:
        raise ValueError("stdout must be PIPE or DEVNULL")
    if stderr not in {subprocess.PIPE, subprocess.DEVNULL}:
        raise ValueError("stderr must be PIPE or DEVNULL")
    if stdout_limit < 0 or stderr_limit < 0:
        raise ValueError("stream limits must be nonnegative")
    if input_data is not None and not isinstance(input_data, bytes):
        raise TypeError("input_data must be bytes")
    if input_data is not None and stdin != subprocess.DEVNULL:
        raise ValueError("input_data owns the child stdin pipe")

    active_platform = sys.platform if _platform is None else _platform
    scope_factory = _scope_factory or _make_process_scope
    try:
        scope = scope_factory(active_platform)
    except Exception:
        raise _runner_failed() from None

    try:
        popen_kwargs = dict(scope.popen_kwargs)
        process = subprocess.Popen(
            list(args),
            shell=False,
            stdin=subprocess.PIPE if input_data is not None else stdin,
            stdout=stdout,
            stderr=stderr,
            env=None if env is None else dict(env),
            **popen_kwargs,
        )
    except Exception:
        try:
            scope.close()
        except Exception:
            pass
        raise _runner_failed() from None

    def close_process_streams() -> bool:
        failed = False
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:
                failed = True
        return failed

    def kill_and_wait_direct_child() -> bool:
        failed = False
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
        except Exception:
            failed = True
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass
            except Exception:
                failed = True
            try:
                process.wait()
            except Exception:
                failed = True
        except Exception:
            failed = True
        return failed

    try:
        scope.attach(process)
    except Exception:
        kill_and_wait_direct_child()
        close_process_streams()
        try:
            scope.close()
        except Exception:
            pass
        raise _runner_failed() from None

    captured_stdout = bytearray()
    captured_stderr = bytearray()
    overflow = threading.Event()
    reader_failed = threading.Event()
    writer_failed = threading.Event()
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
    input_writer: threading.Thread | None = None
    if input_data is not None:
        assert process.stdin is not None

        def write_input() -> None:
            try:
                view = memoryview(input_data)
                while view:
                    written = os.write(process.stdin.fileno(), view)
                    if written <= 0:
                        raise OSError("child stdin is unavailable")
                    view = view[written:]
            except (BrokenPipeError, OSError):
                pass
            except Exception:
                writer_failed.set()
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass
                except Exception:
                    writer_failed.set()

        input_writer = threading.Thread(
            target=write_input,
            name="bounded-process-input-writer",
            daemon=True,
        )
        input_writer.start()

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
            if writer_failed.is_set():
                failure_code = "runner_failed"
                break
            try:
                process_exited = scope.direct_child_exited()
            except Exception:
                failure_code = "runner_failed"
                break
            workers_exited = all(not reader.is_alive() for reader in readers) and (
                input_writer is None or not input_writer.is_alive()
            )
            if process_exited and workers_exited:
                break
            if time.monotonic() >= deadline:
                failure_code = "timeout"
                break
            time.sleep(0.01)
    finally:
        try:
            scope.terminate()
        except Exception:
            failure_code = "runner_failed"
        try:
            scope.close()
        except Exception:
            failure_code = "runner_failed"
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if kill_and_wait_direct_child():
                failure_code = "runner_failed"
        except Exception:
            kill_and_wait_direct_child()
            failure_code = "runner_failed"
        if close_process_streams():
            failure_code = "runner_failed"
        for reader in readers:
            reader.join(timeout=1)
            if reader.is_alive():
                failure_code = "runner_failed"
        if input_writer is not None:
            input_writer.join(timeout=1)
            if input_writer.is_alive():
                failure_code = "runner_failed"

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
