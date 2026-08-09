import importlib
import inspect
import subprocess
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from openusage_bar import bounded_process
from openusage_bar.bounded_process import BoundedProcessError


CREATE_NEW_PROCESS_GROUP = getattr(
    subprocess,
    "CREATE_NEW_PROCESS_GROUP",
    0x00000200,
)


class FakeKernel32:
    def __init__(self, failure=None, *, terminate=True, close=True):
        self.failure = failure
        self.terminate_result = terminate
        self.close_result = close
        self.calls = []

    def create_job(self):
        self.calls.append(("create",))
        return None if self.failure == "create" else 41

    def set_kill_on_job_close(self, job_handle):
        self.calls.append(("set_kill_on_job_close", job_handle))
        return self.failure != "set"

    def assign_process(self, job_handle, process_handle):
        self.calls.append(("assign_process", job_handle, process_handle))
        return self.failure != "assign"

    def terminate_job(self, job_handle):
        self.calls.append(("terminate_job", job_handle))
        return self.terminate_result

    def close_handle(self, job_handle):
        self.calls.append(("close_handle", job_handle))
        return self.close_result


class FakeProcess:
    def __init__(self, *, stdin=None, stdout=None, stderr=None, poll_result=0):
        self._handle = 73
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = 0
        self.poll_result = poll_result
        self.poll_calls = 0
        self.kill_calls = 0
        self.wait_calls = []

    def poll(self):
        self.poll_calls += 1
        return self.poll_result

    def kill(self):
        self.kill_calls += 1

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.returncode


class FakeStream:
    def __init__(self, *, on_close=None):
        self.close_calls = 0
        self.on_close = on_close

    def fileno(self):
        return 91

    def close(self):
        self.close_calls += 1
        if self.on_close is not None:
            self.on_close()


class FakeJob:
    def __init__(self):
        self.calls = []

    def assign(self, process):
        self.calls.append(("assign", process._handle))

    def terminate(self):
        self.calls.append(("terminate",))

    def close(self):
        self.calls.append(("close",))


class FakeScope:
    popen_kwargs = {"creationflags": CREATE_NEW_PROCESS_GROUP}

    def __init__(self, *, exited, attach_error=None):
        self.exited = exited
        self.attach_error = attach_error
        self.calls = []

    def attach(self, process):
        self.calls.append(("attach", process))
        if self.attach_error is not None:
            raise self.attach_error

    def direct_child_exited(self):
        self.calls.append(("poll",))
        return self.exited

    def terminate(self):
        self.calls.append(("terminate",))

    def close(self):
        self.calls.append(("close",))


class WindowsJobTests(unittest.TestCase):
    def windows_job_factory(self):
        try:
            module = importlib.import_module("openusage_bar._windows_job")
        except ModuleNotFoundError:
            self.fail("missing private openusage_bar._windows_job module")
        factory = getattr(module, "create_windows_job", None)
        self.assertIsNotNone(factory, "missing create_windows_job factory")
        return factory

    def test_job_lifecycle_and_each_setup_failure_are_fail_closed(self):
        create_windows_job = self.windows_job_factory()
        kernel32 = FakeKernel32()
        process = FakeProcess()

        job = create_windows_job(kernel32=kernel32)
        job.assign(process)
        job.terminate()
        job.terminate()
        job.close()
        job.close()

        self.assertEqual(
            kernel32.calls,
            [
                ("create",),
                ("set_kill_on_job_close", 41),
                ("assign_process", 41, 73),
                ("terminate_job", 41),
                ("close_handle", 41),
            ],
        )

        for failure, expected_calls in (
            ("create", [("create",)]),
            (
                "set",
                [
                    ("create",),
                    ("set_kill_on_job_close", 41),
                    ("close_handle", 41),
                ],
            ),
            (
                "assign",
                [
                    ("create",),
                    ("set_kill_on_job_close", 41),
                    ("assign_process", 41, 73),
                    ("close_handle", 41),
                ],
            ),
        ):
            with self.subTest(failure=failure):
                failing_kernel32 = FakeKernel32(failure)
                failing_process = FakeProcess()
                with self.assertRaises(BoundedProcessError) as raised:
                    failing_job = create_windows_job(kernel32=failing_kernel32)
                    if failure == "assign":
                        failing_job.assign(failing_process)

                self.assertEqual(raised.exception.code, "runner_failed")
                self.assertEqual(failing_kernel32.calls, expected_calls)
                self.assertEqual(failing_process.kill_calls, 0)

    def test_terminate_and_close_failures_release_the_handle_exactly_once(self):
        create_windows_job = self.windows_job_factory()

        terminate_kernel32 = FakeKernel32(terminate=False, close=False)
        terminate_job = create_windows_job(kernel32=terminate_kernel32)
        terminate_job.assign(FakeProcess())
        with self.assertRaises(BoundedProcessError) as raised:
            terminate_job.terminate()
        self.assertEqual(raised.exception.code, "runner_failed")

        terminate_job.terminate()
        terminate_job.close()
        terminate_job.close()
        self.assertEqual(
            terminate_kernel32.calls,
            [
                ("create",),
                ("set_kill_on_job_close", 41),
                ("assign_process", 41, 73),
                ("terminate_job", 41),
                ("close_handle", 41),
            ],
        )

        close_kernel32 = FakeKernel32(close=False)
        close_job = create_windows_job(kernel32=close_kernel32)
        with self.assertRaises(BoundedProcessError) as raised:
            close_job.close()
        self.assertEqual(raised.exception.code, "runner_failed")

        close_job.close()
        self.assertEqual(
            close_kernel32.calls,
            [
                ("create",),
                ("set_kill_on_job_close", 41),
                ("close_handle", 41),
            ],
        )


class ProcessScopeTests(unittest.TestCase):
    def process_scope_factory(self):
        factory = getattr(bounded_process, "_make_process_scope", None)
        self.assertIsNotNone(factory, "missing injectable platform scope factory")
        return factory

    def test_platform_scopes_use_only_their_native_process_primitives(self):
        make_scope = self.process_scope_factory()
        posix_scope = make_scope("darwin")
        self.assertEqual(posix_scope.popen_kwargs, {"start_new_session": True})

        job = FakeJob()
        process = FakeProcess(poll_result=0)
        windows_scope = make_scope(
            "win32",
            windows_job_factory=lambda: job,
        )
        self.assertEqual(
            windows_scope.popen_kwargs,
            {"creationflags": CREATE_NEW_PROCESS_GROUP},
        )

        forbidden = AssertionError("Windows scope used a POSIX process primitive")
        with (
            patch.object(
                bounded_process.os,
                "waitid",
                side_effect=forbidden,
                create=True,
            ) as waitid,
            patch.object(
                bounded_process.os,
                "getpgrp",
                side_effect=forbidden,
                create=True,
            ) as getpgrp,
            patch.object(
                bounded_process.os,
                "killpg",
                side_effect=forbidden,
                create=True,
            ) as killpg,
        ):
            windows_scope.attach(process)
            self.assertTrue(windows_scope.direct_child_exited())
            windows_scope.terminate()
            windows_scope.close()

        waitid.assert_not_called()
        getpgrp.assert_not_called()
        killpg.assert_not_called()
        self.assertEqual(process.poll_calls, 1)
        self.assertEqual(process.kill_calls, 0)
        self.assertEqual(
            job.calls,
            [("assign", 73), ("terminate",), ("close",)],
        )

    def test_scope_setup_and_attach_failures_are_fail_closed(self):
        parameters = inspect.signature(bounded_process.run_bounded).parameters
        self.assertIn("_platform", parameters)
        self.assertIn("_scope_factory", parameters)

        popen = Mock()
        scope_factory = Mock(side_effect=BoundedProcessError("runner_failed"))
        with (
            patch.object(bounded_process.subprocess, "Popen", popen),
            self.assertRaises(BoundedProcessError) as raised,
        ):
            bounded_process.run_bounded(
                ["fake-exporter"],
                timeout=1,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                _platform="win32",
                _scope_factory=scope_factory,
            )

        self.assertEqual(raised.exception.code, "runner_failed")
        self.assertNotIn("fake-exporter", str(raised.exception))
        scope_factory.assert_called_once_with("win32")
        popen.assert_not_called()

        process = FakeProcess(poll_result=None)
        scope = FakeScope(
            exited=False,
            attach_error=BoundedProcessError("runner_failed"),
        )
        popen = Mock(return_value=process)
        with (
            patch.object(bounded_process.subprocess, "Popen", popen),
            self.assertRaises(BoundedProcessError) as raised,
        ):
            bounded_process.run_bounded(
                ["fake-exporter"],
                timeout=1,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                _platform="win32",
                _scope_factory=lambda _platform: scope,
            )

        self.assertEqual(raised.exception.code, "runner_failed")
        self.assertNotIn("fake-exporter", str(raised.exception))
        self.assertEqual(scope.calls[0], ("attach", process))
        self.assertEqual(scope.calls.count(("terminate",)), 0)
        self.assertEqual(scope.calls.count(("close",)), 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertGreaterEqual(len(process.wait_calls), 1)

    def test_all_runner_outcomes_terminate_and_close_the_injected_scope(self):
        parameters = inspect.signature(bounded_process.run_bounded).parameters
        self.assertIn("_platform", parameters)
        self.assertIn("_scope_factory", parameters)

        for outcome, expected_error in (
            ("success", None),
            ("timeout", "timeout"),
            ("stdout_overflow", "output_overflow"),
            ("stderr_overflow", "output_overflow"),
            ("reader_failure", "reader_failed"),
        ):
            with self.subTest(outcome=outcome):
                stream = (
                    FakeStream()
                    if outcome
                    in {"stdout_overflow", "stderr_overflow", "reader_failure"}
                    else None
                )
                stdout_stream = (
                    stream
                    if outcome in {"stdout_overflow", "reader_failure"}
                    else None
                )
                stderr_stream = stream if outcome == "stderr_overflow" else None
                process = FakeProcess(
                    stdout=stdout_stream,
                    stderr=stderr_stream,
                    poll_result=0 if outcome == "success" else None,
                )
                scope = FakeScope(exited=outcome == "success")
                popen = Mock(return_value=process)

                def read_stream(_stream, _target, _limit, overflow, reader_failed):
                    (
                        overflow
                        if outcome in {"stdout_overflow", "stderr_overflow"}
                        else reader_failed
                    ).set()

                with ExitStack() as stack:
                    stack.enter_context(
                        patch.object(bounded_process.subprocess, "Popen", popen)
                    )
                    if stream is not None:
                        stack.enter_context(
                            patch.object(
                                bounded_process,
                                "_read_stream",
                                new=read_stream,
                            )
                        )
                    if outcome == "timeout":
                        stack.enter_context(
                            patch.object(
                                bounded_process.time,
                                "monotonic",
                                side_effect=[0.0, 2.0],
                            )
                        )

                    call = lambda: bounded_process.run_bounded(
                        ["fake-exporter"],
                        timeout=1,
                        stdout=(
                            subprocess.PIPE
                            if stdout_stream is not None
                            else subprocess.DEVNULL
                        ),
                        stderr=(
                            subprocess.PIPE
                            if stderr_stream is not None
                            else subprocess.DEVNULL
                        ),
                        _platform="win32",
                        _scope_factory=lambda platform: scope,
                    )
                    if expected_error is None:
                        self.assertEqual(call().returncode, 0)
                    else:
                        with self.assertRaises(BoundedProcessError) as raised:
                            call()
                        self.assertEqual(raised.exception.code, expected_error)

                popen_options = {
                    key: value
                    for key, value in popen.call_args.kwargs.items()
                    if key in {"start_new_session", "creationflags"}
                }
                self.assertEqual(
                    popen_options,
                    {"creationflags": CREATE_NEW_PROCESS_GROUP},
                )
                self.assertEqual(scope.calls[0], ("attach", process))
                self.assertEqual(scope.calls.count(("terminate",)), 1)
                self.assertEqual(scope.calls.count(("close",)), 1)
                self.assertLess(
                    scope.calls.index(("terminate",)),
                    scope.calls.index(("close",)),
                )
                self.assertEqual(process.kill_calls, 0)

    def test_unexpected_input_writer_failure_is_sanitized_and_cleans_scope(self):
        input_stream = FakeStream()
        process = FakeProcess(stdin=input_stream, poll_result=None)
        scope = FakeScope(exited=False)
        write_started = threading.Event()

        def fail_write(_file_descriptor, _payload):
            write_started.set()
            raise RuntimeError("injected writer boundary failure")

        with (
            patch.object(bounded_process.subprocess, "Popen", return_value=process),
            patch.object(bounded_process.os, "write", new=fail_write),
            self.assertRaises(BoundedProcessError) as raised,
        ):
            bounded_process.run_bounded(
                ["fake-exporter"],
                timeout=1,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                input_data=b"private-input",
                _platform="win32",
                _scope_factory=lambda _platform: scope,
            )

        self.assertEqual(raised.exception.code, "runner_failed")
        self.assertTrue(write_started.is_set())
        self.assertEqual(scope.calls.count(("terminate",)), 1)
        self.assertEqual(scope.calls.count(("close",)), 1)
        self.assertFalse(
            any(
                thread.name == "bounded-process-input-writer" and thread.is_alive()
                for thread in threading.enumerate()
            )
        )

    def test_blocked_input_writer_is_released_during_timeout_cleanup(self):
        write_started = threading.Event()
        release_write = threading.Event()
        input_stream = FakeStream(on_close=release_write.set)
        process = FakeProcess(stdin=input_stream, poll_result=None)
        scope = FakeScope(exited=False)

        def block_write(_file_descriptor, payload):
            write_started.set()
            if not release_write.wait(0.5):
                raise RuntimeError("injected writer cleanup did not release")
            return len(payload)

        with (
            patch.object(bounded_process.subprocess, "Popen", return_value=process),
            patch.object(bounded_process.os, "write", new=block_write),
            patch.object(
                bounded_process.time,
                "monotonic",
                side_effect=[0.0, 2.0],
            ),
            self.assertRaises(BoundedProcessError) as raised,
        ):
            bounded_process.run_bounded(
                ["fake-exporter"],
                timeout=1,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                input_data=b"private-input",
                _platform="win32",
                _scope_factory=lambda _platform: scope,
            )

        self.assertEqual(raised.exception.code, "timeout")
        self.assertTrue(write_started.is_set())
        self.assertTrue(release_write.is_set())
        self.assertEqual(scope.calls.count(("terminate",)), 1)
        self.assertEqual(scope.calls.count(("close",)), 1)
        self.assertFalse(
            any(
                thread.name == "bounded-process-input-writer" and thread.is_alive()
                for thread in threading.enumerate()
            )
        )


if __name__ == "__main__":
    unittest.main()
