import ctypes
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from openusage_bar.bounded_process import BoundedProcessError, run_bounded


CREATE_NEW_PROCESS_GROUP = getattr(
    subprocess,
    "CREATE_NEW_PROCESS_GROUP",
    0x00000200,
)
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102

_GRANDCHILD_SOURCE = (
    "import os\n"
    "import sys\n"
    "import time\n"
    "with open(sys.argv[1], 'w', encoding='ascii') as stream:\n"
    "    stream.write(str(os.getpid()))\n"
    "if sys.argv[2] == 'close_stdio':\n"
    "    for descriptor in (0, 1, 2):\n"
    "        try:\n"
    "            os.close(descriptor)\n"
    "        except OSError:\n"
    "            pass\n"
    "time.sleep(30)\n"
)

_NATIVE_HELPER_SOURCE = (
    "import os\n"
    "import subprocess\n"
    "import sys\n"
    "import time\n"
    "mode, grandchild_pid_path, parent_pid_path, trigger_path = sys.argv[1:5]\n"
    "with open(parent_pid_path, 'w', encoding='ascii') as stream:\n"
    "    stream.write(str(os.getpid()))\n"
    f"grandchild_source = {_GRANDCHILD_SOURCE!r}\n"
    "grandchild_mode = 'close_stdio' if mode == 'close_stdio' else 'hold_stdio'\n"
    "subprocess.Popen(\n"
    "    [sys.executable, '-c', grandchild_source, grandchild_pid_path, grandchild_mode],\n"
    "    stdin=subprocess.DEVNULL,\n"
    ")\n"
    "while not os.path.exists(trigger_path):\n"
    "    time.sleep(0.01)\n"
    "if mode == 'stdout_overflow':\n"
    "    sys.stdout.buffer.write(b'x' * 131072)\n"
    "    sys.stdout.buffer.flush()\n"
    "elif mode == 'stderr_overflow':\n"
    "    sys.stderr.buffer.write(b'x' * 131072)\n"
    "    sys.stderr.buffer.flush()\n"
    "elif mode in {'parent_exit_pipe', 'close_stdio'}:\n"
    "    raise SystemExit(0)\n"
    "time.sleep(30)\n"
)


@unittest.skipUnless(sys.platform == "win32", "native Windows Job Object evidence")
class NativeWindowsJobEvidenceTests(unittest.TestCase):
    CALL_TIMEOUT_SECONDS = 6.0
    PUBLISH_TIMEOUT_SECONDS = 5.0
    CALL_JOIN_SECONDS = 15.0
    HANDLE_WAIT_MILLISECONDS = 6000

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        cls.kernel32.OpenProcess.argtypes = (
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        )
        cls.kernel32.OpenProcess.restype = ctypes.c_void_p
        cls.kernel32.WaitForSingleObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        cls.kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        cls.kernel32.TerminateProcess.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
        )
        cls.kernel32.TerminateProcess.restype = ctypes.c_int
        cls.kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        cls.kernel32.CloseHandle.restype = ctypes.c_int

    def _write_helper(self, root: Path) -> Path:
        helper = root / "native_job_helper.py"
        helper.write_text(_NATIVE_HELPER_SOURCE, encoding="utf-8")
        return helper

    @staticmethod
    def _read_pid(path: Path) -> int | None:
        try:
            value = int(path.read_text(encoding="ascii"))
        except (FileNotFoundError, OSError, ValueError):
            return None
        return value if value > 0 else None

    def _wait_for_pid(self, path: Path, worker: threading.Thread) -> int:
        deadline = time.monotonic() + self.PUBLISH_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            pid = self._read_pid(path)
            if pid is not None:
                return pid
            if not worker.is_alive():
                break
            time.sleep(0.01)
        self.fail("native helper did not publish process evidence")

    def _open_synchronize_handle(self, pid: int) -> int:
        handle = self.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        self.assertTrue(handle, "OpenProcess(SYNCHRONIZE) failed")
        return int(handle)

    def _wait_for_handle(self, handle: int, milliseconds: int) -> int:
        return int(self.kernel32.WaitForSingleObject(handle, milliseconds))

    def _force_terminate_pid(self, pid: int | None) -> None:
        if pid is None:
            return
        handle = self.kernel32.OpenProcess(
            PROCESS_TERMINATE | SYNCHRONIZE,
            False,
            pid,
        )
        if not handle:
            return
        try:
            self.kernel32.TerminateProcess(handle, 1)
            self.kernel32.WaitForSingleObject(
                handle,
                self.HANDLE_WAIT_MILLISECONDS,
            )
        finally:
            self.kernel32.CloseHandle(handle)

    def _finish_worker_cleanup(
        self,
        worker: threading.Thread,
        trigger: Path,
        parent_pid_path: Path,
        grandchild_pid_path: Path,
    ) -> None:
        try:
            trigger.touch(exist_ok=True)
        except OSError:
            pass
        worker.join(1.0)
        if worker.is_alive():
            self._force_terminate_pid(self._read_pid(parent_pid_path))
            self._force_terminate_pid(self._read_pid(grandchild_pid_path))
            worker.join(self.HANDLE_WAIT_MILLISECONDS / 1000)

    def _run_grandchild_case(
        self,
        mode: str,
        *,
        stdout_limit: int = 1024 * 1024,
        stderr_limit: int = 64 * 1024,
    ) -> dict[str, object]:
        outcome: dict[str, object] = {}
        call_was_running = False
        call_finished = False
        wait_result: int | None = None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = self._write_helper(root)
            grandchild_pid_path = root / "grandchild.pid"
            parent_pid_path = root / "parent.pid"
            trigger = root / "continue"

            def invoke() -> None:
                try:
                    outcome["result"] = run_bounded(
                        [
                            sys.executable,
                            str(helper),
                            mode,
                            str(grandchild_pid_path),
                            str(parent_pid_path),
                            str(trigger),
                        ],
                        timeout=self.CALL_TIMEOUT_SECONDS,
                        stdout_limit=stdout_limit,
                        stderr_limit=stderr_limit,
                    )
                except BaseException as error:
                    outcome["error"] = error

            worker = threading.Thread(
                target=invoke,
                name="native-bounded-process-evidence",
                daemon=True,
            )
            grandchild_handle: int | None = None
            grandchild_pid: int | None = None
            worker.start()
            try:
                grandchild_pid = self._wait_for_pid(grandchild_pid_path, worker)
                grandchild_handle = self._open_synchronize_handle(grandchild_pid)
                call_was_running = worker.is_alive()
                trigger.touch()
                worker.join(self.CALL_JOIN_SECONDS)
                call_finished = not worker.is_alive()
                if call_finished:
                    wait_result = self._wait_for_handle(
                        grandchild_handle,
                        self.HANDLE_WAIT_MILLISECONDS,
                    )
            finally:
                self._finish_worker_cleanup(
                    worker,
                    trigger,
                    parent_pid_path,
                    grandchild_pid_path,
                )
                if grandchild_handle is not None:
                    if self._wait_for_handle(grandchild_handle, 0) == WAIT_TIMEOUT:
                        self._force_terminate_pid(grandchild_pid)
                        self._wait_for_handle(
                            grandchild_handle,
                            self.HANDLE_WAIT_MILLISECONDS,
                        )
                    self.kernel32.CloseHandle(grandchild_handle)

        self.assertTrue(call_was_running, "bounded call finished before evidence hold")
        self.assertTrue(call_finished, "bounded call exceeded its cleanup allowance")
        self.assertEqual(wait_result, WAIT_OBJECT_0)
        return outcome

    def assert_bounded_failure(self, outcome: dict[str, object], code: str) -> None:
        self.assertEqual(getattr(outcome.get("error"), "code", None), code)

    def test_real_job_create_configure_assign_terminate_and_close(self):
        from openusage_bar._windows_job import create_windows_job

        job = None
        process = None
        process_handle = None
        wait_result = None
        try:
            job = create_windows_job()
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NEW_PROCESS_GROUP,
            )
            process_handle = self._open_synchronize_handle(process.pid)
            job.assign(process)
            job.terminate()
            job.close()
            wait_result = self._wait_for_handle(
                process_handle,
                self.HANDLE_WAIT_MILLISECONDS,
            )
        finally:
            if job is not None:
                try:
                    job.close()
                except BoundedProcessError:
                    pass
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=self.HANDLE_WAIT_MILLISECONDS / 1000)
            if process_handle is not None:
                self.kernel32.CloseHandle(process_handle)

        self.assertEqual(wait_result, WAIT_OBJECT_0)

    def test_timeout_and_each_stream_overflow_reap_grandchild(self):
        cases = (
            ("timeout", "timeout", 1024 * 1024, 64 * 1024),
            ("stdout_overflow", "output_overflow", 4096, 64 * 1024),
            ("stderr_overflow", "output_overflow", 1024 * 1024, 4096),
        )
        for mode, code, stdout_limit, stderr_limit in cases:
            with self.subTest(mode=mode):
                outcome = self._run_grandchild_case(
                    mode,
                    stdout_limit=stdout_limit,
                    stderr_limit=stderr_limit,
                )
                self.assert_bounded_failure(outcome, code)

    def test_parent_exit_with_grandchild_holding_pipes_times_out_and_reaps(self):
        outcome = self._run_grandchild_case("parent_exit_pipe")
        self.assert_bounded_failure(outcome, "timeout")

    def test_parent_success_with_closed_grandchild_stdio_still_reaps_job(self):
        outcome = self._run_grandchild_case("close_stdio")
        self.assertEqual(getattr(outcome.get("result"), "returncode", None), 0)

    def test_unrelated_sentinel_remains_alive(self):
        sentinel = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NEW_PROCESS_GROUP,
        )
        sentinel_handle = None
        result_code = None
        wait_result = None
        try:
            sentinel_handle = self._open_synchronize_handle(sentinel.pid)
            result = run_bounded(
                [sys.executable, "-c", "raise SystemExit(0)"],
                timeout=self.CALL_TIMEOUT_SECONDS,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            result_code = result.returncode
            wait_result = self._wait_for_handle(sentinel_handle, 0)
        finally:
            if sentinel.poll() is None:
                sentinel.kill()
            sentinel.wait(timeout=self.HANDLE_WAIT_MILLISECONDS / 1000)
            if sentinel_handle is not None:
                self.kernel32.CloseHandle(sentinel_handle)

        self.assertEqual(result_code, 0)
        self.assertEqual(wait_result, WAIT_TIMEOUT)

    def test_blocked_large_input_writer_is_gone_after_timeout(self):
        with self.assertRaises(BoundedProcessError) as raised:
            run_bounded(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=self.CALL_TIMEOUT_SECONDS,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                input_data=b"x" * (16 * 1024 * 1024),
            )

        self.assertEqual(raised.exception.code, "timeout")
        self.assertFalse(
            any(
                thread.name == "bounded-process-input-writer" and thread.is_alive()
                for thread in threading.enumerate()
            )
        )


if __name__ == "__main__":
    unittest.main()
