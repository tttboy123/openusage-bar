from __future__ import annotations

import ctypes
import sys
from typing import Any

from .bounded_process import BoundedProcessError


_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_TERMINATION_EXIT_CODE = 1


# Use fixed-width Win32 scalar types so this private module remains importable
# for contract tests on non-Windows hosts without changing the native ABI.
_BOOL = ctypes.c_int32
_DWORD = ctypes.c_uint32
_HANDLE = ctypes.c_void_p
_LARGE_INTEGER = ctypes.c_int64
_SIZE_T = ctypes.c_size_t
_ULONG_PTR = ctypes.c_size_t


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", _LARGE_INTEGER),
        ("PerJobUserTimeLimit", _LARGE_INTEGER),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", _SIZE_T),
        ("MaximumWorkingSetSize", _SIZE_T),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", _ULONG_PTR),
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    )


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = (
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    )


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", _SIZE_T),
        ("JobMemoryLimit", _SIZE_T),
        ("PeakProcessMemoryUsed", _SIZE_T),
        ("PeakJobMemoryUsed", _SIZE_T),
    )


def _runner_failed() -> BoundedProcessError:
    return BoundedProcessError("runner_failed")


class _NativeKernel32:
    def __init__(self, library: Any) -> None:
        self._create_job = library.CreateJobObjectW
        self._create_job.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        self._create_job.restype = _HANDLE

        self._set_job_information = library.SetInformationJobObject
        self._set_job_information.argtypes = (
            _HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            _DWORD,
        )
        self._set_job_information.restype = _BOOL

        self._assign_process = library.AssignProcessToJobObject
        self._assign_process.argtypes = (_HANDLE, _HANDLE)
        self._assign_process.restype = _BOOL

        self._terminate_job = library.TerminateJobObject
        self._terminate_job.argtypes = (_HANDLE, _DWORD)
        self._terminate_job.restype = _BOOL

        self._close_handle = library.CloseHandle
        self._close_handle.argtypes = (_HANDLE,)
        self._close_handle.restype = _BOOL

    def create_job(self) -> int | None:
        handle = self._create_job(None, None)
        return int(handle) if handle else None

    def set_kill_on_job_close(self, job_handle: int) -> bool:
        information = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        return bool(
            self._set_job_information(
                job_handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(information),
                ctypes.sizeof(information),
            )
        )

    def assign_process(self, job_handle: int, process_handle: int) -> bool:
        return bool(self._assign_process(job_handle, process_handle))

    def terminate_job(self, job_handle: int) -> bool:
        return bool(self._terminate_job(job_handle, _TERMINATION_EXIT_CODE))

    def close_handle(self, job_handle: int) -> bool:
        return bool(self._close_handle(job_handle))


def _load_kernel32() -> _NativeKernel32:
    if sys.platform != "win32":
        raise _runner_failed()
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise _runner_failed()
    try:
        return _NativeKernel32(loader("kernel32", use_last_error=True))
    except Exception:
        raise _runner_failed() from None


class _WindowsJob:
    def __init__(self, kernel32: Any, handle: int) -> None:
        self._kernel32 = kernel32
        self._handle: int | None = handle
        self._assigned = False
        self._terminated = False

    def assign(self, process: Any) -> None:
        if self._handle is None or self._assigned:
            raise _runner_failed()
        try:
            process_handle = int(process._handle)
            assigned = bool(
                process_handle
                and self._kernel32.assign_process(self._handle, process_handle)
            )
        except Exception:
            assigned = False
        if not assigned:
            try:
                self.close()
            except BoundedProcessError:
                pass
            raise _runner_failed() from None
        self._assigned = True

    def terminate(self) -> None:
        if self._handle is None or self._terminated:
            return
        try:
            terminated = bool(self._kernel32.terminate_job(self._handle))
        except Exception:
            terminated = False
        self._terminated = True
        if not terminated:
            try:
                self.close()
            except BoundedProcessError:
                pass
            raise _runner_failed() from None

    def close(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        try:
            closed = bool(self._kernel32.close_handle(handle))
        except Exception:
            closed = False
        if not closed:
            raise _runner_failed() from None


def create_windows_job(kernel32: Any | None = None) -> _WindowsJob:
    """Create a kill-on-close Job Object without exposing native diagnostics."""

    api = kernel32
    if api is None:
        api = _load_kernel32()
    try:
        handle = api.create_job()
    except Exception:
        raise _runner_failed() from None
    if not handle:
        raise _runner_failed()

    job = _WindowsJob(api, int(handle))
    try:
        configured = bool(api.set_kill_on_job_close(int(handle)))
    except Exception:
        configured = False
    if not configured:
        try:
            job.close()
        except BoundedProcessError:
            pass
        raise _runner_failed() from None
    return job
