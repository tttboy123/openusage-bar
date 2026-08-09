"""Windows ACL boundary for local bearer-token files.

The native implementation is deliberately unavailable off Windows.  Callers
depend on the small protocol below so non-Windows CI can inject a fake seam and
exercise lifecycle ordering and failure behavior without claiming that it has
verified native Windows ACL semantics.
"""

from __future__ import annotations

import ctypes
import os
import threading
from pathlib import Path
from typing import Protocol


class WindowsFileSecurity(Protocol):
    """Harden filesystem objects to the current user and LocalSystem only."""

    def harden_directory(self, directory: Path) -> None: ...

    def harden_file(self, descriptor: int) -> None: ...

    def verify_file(self, descriptor: int) -> None: ...


_DWORD = ctypes.c_uint32
_WORD = ctypes.c_uint16
_BYTE = ctypes.c_ubyte
_BOOL = ctypes.c_int
_HANDLE = ctypes.c_void_p


class _FILETIME(ctypes.Structure):
    _fields_ = (("low", _DWORD), ("high", _DWORD))


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("attributes", _DWORD),
        ("creation_time", _FILETIME),
        ("last_access_time", _FILETIME),
        ("last_write_time", _FILETIME),
        ("volume_serial_number", _DWORD),
        ("file_size_high", _DWORD),
        ("file_size_low", _DWORD),
        ("number_of_links", _DWORD),
        ("file_index_high", _DWORD),
        ("file_index_low", _DWORD),
    )


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = (("sid", ctypes.c_void_p), ("attributes", _DWORD))


class _TOKEN_USER(ctypes.Structure):
    _fields_ = (("user", _SID_AND_ATTRIBUTES),)


class _ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("ace_count", _DWORD),
        ("bytes_in_use", _DWORD),
        ("bytes_free", _DWORD),
    )


class _ACE_HEADER(ctypes.Structure):
    _fields_ = (
        ("ace_type", _BYTE),
        ("ace_flags", _BYTE),
        ("ace_size", _WORD),
    )


class _ACCESS_ALLOWED_ACE(ctypes.Structure):
    _fields_ = (
        ("header", _ACE_HEADER),
        ("mask", _DWORD),
        ("sid_start", _DWORD),
    )


class _NativeSecurityFailure(Exception):
    pass


class _NativeWindowsFileSecurity:
    """ctypes adapter around handle-based Win32 security APIs."""

    _ERROR = "Windows token security operation failed"
    _ERROR_INSUFFICIENT_BUFFER = 122
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER_CLASS = 1
    _WIN_LOCAL_SYSTEM_SID = 22
    _SECURITY_MAX_SID_SIZE = 68

    _READ_CONTROL = 0x00020000
    _WRITE_DAC = 0x00040000
    _WRITE_OWNER = 0x00080000
    _FILE_READ_ATTRIBUTES = 0x00000080
    _FILE_ALL_ACCESS = 0x001F01FF
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SE_DACL_PROTECTED = 0x1000

    _ACL_REVISION = 2
    _ACL_SIZE_INFORMATION_CLASS = 2
    _ACCESS_ALLOWED_ACE_TYPE = 0
    _OBJECT_INHERIT_ACE = 0x01
    _CONTAINER_INHERIT_ACE = 0x02

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._initialized = False

    def harden_directory(self, directory: Path) -> None:
        try:
            self._ensure_initialized()
            raw_directory = os.fspath(directory)
            if (
                not isinstance(raw_directory, str)
                or not raw_directory
                or "\x00" in raw_directory
                or len(raw_directory) > 32_767
            ):
                raise _NativeSecurityFailure
            handle = self._CreateFileW(
                raw_directory,
                self._security_access(),
                self._share_all(),
                None,
                self._OPEN_EXISTING,
                self._FILE_FLAG_BACKUP_SEMANTICS
                | self._FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            if not self._valid_handle(handle):
                raise _NativeSecurityFailure
            try:
                self._require_expected_kind(handle, is_directory=True)
                self._set_security(handle, is_directory=True)
                self._verify_security(handle, is_directory=True)
            finally:
                if not self._CloseHandle(handle):
                    raise _NativeSecurityFailure
        except Exception:
            raise OSError(self._ERROR) from None

    def harden_file(self, descriptor: int) -> None:
        try:
            self._ensure_initialized()
            if isinstance(descriptor, bool) or not isinstance(descriptor, int):
                raise _NativeSecurityFailure
            import msvcrt

            original = msvcrt.get_osfhandle(descriptor)
            if not self._valid_handle(original):
                raise _NativeSecurityFailure
            security_handle = self._ReOpenFile(
                original,
                self._security_access(),
                self._share_all(),
                self._FILE_FLAG_OPEN_REPARSE_POINT,
            )
            if not self._valid_handle(security_handle):
                raise _NativeSecurityFailure
            try:
                self._require_expected_kind(security_handle, is_directory=False)
                self._set_security(security_handle, is_directory=False)
                # Verify through the caller's already-open token handle.  This
                # is the handle from which bytes will subsequently be read or
                # written, so a failure remains pre-content and fail-closed.
                self._verify_security(original, is_directory=False)
            finally:
                if not self._CloseHandle(security_handle):
                    raise _NativeSecurityFailure
        except Exception:
            raise OSError(self._ERROR) from None

    def verify_file(self, descriptor: int) -> None:
        """Verify an already-open token file without changing its security."""

        try:
            self._ensure_initialized()
            if isinstance(descriptor, bool) or not isinstance(descriptor, int):
                raise _NativeSecurityFailure
            import msvcrt

            handle = msvcrt.get_osfhandle(descriptor)
            if not self._valid_handle(handle):
                raise _NativeSecurityFailure
            self._require_expected_kind(handle, is_directory=False)
            self._verify_security(handle, is_directory=False)
        except Exception:
            raise OSError(self._ERROR) from None

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            try:
                self._initialize_apis()
                self._initialize_sids_and_acls()
            except Exception:
                raise _NativeSecurityFailure from None
            self._initialized = True

    def _initialize_apis(self) -> None:
        self._advapi32 = ctypes.WinDLL("advapi32.dll", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        advapi32 = self._advapi32
        kernel32 = self._kernel32

        self._GetCurrentProcess = kernel32.GetCurrentProcess
        self._GetCurrentProcess.argtypes = []
        self._GetCurrentProcess.restype = _HANDLE

        self._OpenProcessToken = advapi32.OpenProcessToken
        self._OpenProcessToken.argtypes = [_HANDLE, _DWORD, ctypes.POINTER(_HANDLE)]
        self._OpenProcessToken.restype = _BOOL

        self._GetTokenInformation = advapi32.GetTokenInformation
        self._GetTokenInformation.argtypes = [
            _HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            _DWORD,
            ctypes.POINTER(_DWORD),
        ]
        self._GetTokenInformation.restype = _BOOL

        self._GetLengthSid = advapi32.GetLengthSid
        self._GetLengthSid.argtypes = [ctypes.c_void_p]
        self._GetLengthSid.restype = _DWORD

        self._CopySid = advapi32.CopySid
        self._CopySid.argtypes = [_DWORD, ctypes.c_void_p, ctypes.c_void_p]
        self._CopySid.restype = _BOOL

        self._CreateWellKnownSid = advapi32.CreateWellKnownSid
        self._CreateWellKnownSid.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(_DWORD),
        ]
        self._CreateWellKnownSid.restype = _BOOL

        self._InitializeAcl = advapi32.InitializeAcl
        self._InitializeAcl.argtypes = [ctypes.c_void_p, _DWORD, _DWORD]
        self._InitializeAcl.restype = _BOOL

        self._AddAccessAllowedAceEx = advapi32.AddAccessAllowedAceEx
        self._AddAccessAllowedAceEx.argtypes = [
            ctypes.c_void_p,
            _DWORD,
            _DWORD,
            _DWORD,
            ctypes.c_void_p,
        ]
        self._AddAccessAllowedAceEx.restype = _BOOL

        self._SetSecurityInfo = advapi32.SetSecurityInfo
        self._SetSecurityInfo.argtypes = [
            _HANDLE,
            _DWORD,
            _DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._SetSecurityInfo.restype = _DWORD

        self._GetSecurityInfo = advapi32.GetSecurityInfo
        self._GetSecurityInfo.argtypes = [
            _HANDLE,
            _DWORD,
            _DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._GetSecurityInfo.restype = _DWORD

        self._GetSecurityDescriptorControl = advapi32.GetSecurityDescriptorControl
        self._GetSecurityDescriptorControl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_WORD),
            ctypes.POINTER(_DWORD),
        ]
        self._GetSecurityDescriptorControl.restype = _BOOL

        self._GetAclInformation = advapi32.GetAclInformation
        self._GetAclInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            _DWORD,
            ctypes.c_int,
        ]
        self._GetAclInformation.restype = _BOOL

        self._GetAce = advapi32.GetAce
        self._GetAce.argtypes = [
            ctypes.c_void_p,
            _DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._GetAce.restype = _BOOL

        self._EqualSid = advapi32.EqualSid
        self._EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._EqualSid.restype = _BOOL

        self._IsValidSid = advapi32.IsValidSid
        self._IsValidSid.argtypes = [ctypes.c_void_p]
        self._IsValidSid.restype = _BOOL

        self._CreateFileW = kernel32.CreateFileW
        self._CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            _DWORD,
            _DWORD,
            ctypes.c_void_p,
            _DWORD,
            _DWORD,
            _HANDLE,
        ]
        self._CreateFileW.restype = _HANDLE

        self._ReOpenFile = kernel32.ReOpenFile
        self._ReOpenFile.argtypes = [_HANDLE, _DWORD, _DWORD, _DWORD]
        self._ReOpenFile.restype = _HANDLE

        self._GetFileInformationByHandle = kernel32.GetFileInformationByHandle
        self._GetFileInformationByHandle.argtypes = [
            _HANDLE,
            ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
        ]
        self._GetFileInformationByHandle.restype = _BOOL

        self._CloseHandle = kernel32.CloseHandle
        self._CloseHandle.argtypes = [_HANDLE]
        self._CloseHandle.restype = _BOOL

        self._LocalFree = kernel32.LocalFree
        self._LocalFree.argtypes = [ctypes.c_void_p]
        self._LocalFree.restype = ctypes.c_void_p

    def _initialize_sids_and_acls(self) -> None:
        token = _HANDLE()
        if not self._OpenProcessToken(
            self._GetCurrentProcess(),
            self._TOKEN_QUERY,
            ctypes.byref(token),
        ):
            raise _NativeSecurityFailure
        try:
            required = _DWORD()
            if self._GetTokenInformation(
                token,
                self._TOKEN_USER_CLASS,
                None,
                0,
                ctypes.byref(required),
            ):
                raise _NativeSecurityFailure
            if (
                ctypes.get_last_error() != self._ERROR_INSUFFICIENT_BUFFER
                or not ctypes.sizeof(_TOKEN_USER) <= required.value <= 65_536
            ):
                raise _NativeSecurityFailure
            token_user_buffer = ctypes.create_string_buffer(required.value)
            if not self._GetTokenInformation(
                token,
                self._TOKEN_USER_CLASS,
                token_user_buffer,
                required.value,
                ctypes.byref(required),
            ):
                raise _NativeSecurityFailure
            token_user = ctypes.cast(
                token_user_buffer,
                ctypes.POINTER(_TOKEN_USER),
            ).contents
            if not token_user.user.sid or not self._IsValidSid(token_user.user.sid):
                raise _NativeSecurityFailure
            user_sid_size = int(self._GetLengthSid(token_user.user.sid))
            if not 8 <= user_sid_size <= self._SECURITY_MAX_SID_SIZE:
                raise _NativeSecurityFailure
            self._user_sid_buffer = ctypes.create_string_buffer(user_sid_size)
            if not self._CopySid(
                user_sid_size,
                self._user_sid_buffer,
                token_user.user.sid,
            ):
                raise _NativeSecurityFailure
        finally:
            if token.value and not self._CloseHandle(token):
                raise _NativeSecurityFailure

        system_sid_size = _DWORD(self._SECURITY_MAX_SID_SIZE)
        self._system_sid_buffer = ctypes.create_string_buffer(
            self._SECURITY_MAX_SID_SIZE
        )
        if not self._CreateWellKnownSid(
            self._WIN_LOCAL_SYSTEM_SID,
            None,
            self._system_sid_buffer,
            ctypes.byref(system_sid_size),
        ):
            raise _NativeSecurityFailure
        if not 8 <= system_sid_size.value <= self._SECURITY_MAX_SID_SIZE:
            raise _NativeSecurityFailure

        self._user_sid = ctypes.cast(self._user_sid_buffer, ctypes.c_void_p)
        self._system_sid = ctypes.cast(self._system_sid_buffer, ctypes.c_void_p)
        self._file_acl_buffer = self._build_acl(ace_flags=0)
        self._directory_acl_buffer = self._build_acl(
            ace_flags=self._OBJECT_INHERIT_ACE | self._CONTAINER_INHERIT_ACE
        )

    def _build_acl(self, *, ace_flags: int) -> ctypes.Array[ctypes.c_char]:
        sid_sizes = (
            int(self._GetLengthSid(self._user_sid)),
            int(self._GetLengthSid(self._system_sid)),
        )
        acl_size = 8 + sum(
            ctypes.sizeof(_ACCESS_ALLOWED_ACE) - ctypes.sizeof(_DWORD) + size
            for size in sid_sizes
        )
        if not 8 < acl_size <= 65_535:
            raise _NativeSecurityFailure
        buffer = ctypes.create_string_buffer(acl_size)
        if not self._InitializeAcl(buffer, acl_size, self._ACL_REVISION):
            raise _NativeSecurityFailure
        for sid in (self._user_sid, self._system_sid):
            if not self._AddAccessAllowedAceEx(
                buffer,
                self._ACL_REVISION,
                ace_flags,
                self._FILE_ALL_ACCESS,
                sid,
            ):
                raise _NativeSecurityFailure
        return buffer

    def _set_security(self, handle: int, *, is_directory: bool) -> None:
        acl = (
            self._directory_acl_buffer if is_directory else self._file_acl_buffer
        )
        result = self._SetSecurityInfo(
            handle,
            self._SE_FILE_OBJECT,
            self._OWNER_SECURITY_INFORMATION
            | self._DACL_SECURITY_INFORMATION
            | self._PROTECTED_DACL_SECURITY_INFORMATION,
            self._user_sid,
            None,
            acl,
            None,
        )
        if result != 0:
            raise _NativeSecurityFailure

    def _verify_security(self, handle: int, *, is_directory: bool) -> None:
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        security_descriptor = ctypes.c_void_p()
        result = self._GetSecurityInfo(
            handle,
            self._SE_FILE_OBJECT,
            self._OWNER_SECURITY_INFORMATION | self._DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(security_descriptor),
        )
        try:
            if (
                result != 0
                or not security_descriptor.value
                or not owner.value
                or not dacl.value
                or not self._IsValidSid(owner)
                or not self._EqualSid(owner, self._user_sid)
            ):
                raise _NativeSecurityFailure

            control = _WORD()
            revision = _DWORD()
            if not self._GetSecurityDescriptorControl(
                security_descriptor,
                ctypes.byref(control),
                ctypes.byref(revision),
            ) or not control.value & self._SE_DACL_PROTECTED:
                raise _NativeSecurityFailure

            information = _ACL_SIZE_INFORMATION()
            if not self._GetAclInformation(
                dacl,
                ctypes.byref(information),
                ctypes.sizeof(information),
                self._ACL_SIZE_INFORMATION_CLASS,
            ) or information.ace_count != 2:
                raise _NativeSecurityFailure

            expected_flags = (
                self._OBJECT_INHERIT_ACE | self._CONTAINER_INHERIT_ACE
                if is_directory
                else 0
            )
            seen_user = False
            seen_system = False
            for index in range(information.ace_count):
                ace_pointer = ctypes.c_void_p()
                if not self._GetAce(dacl, index, ctypes.byref(ace_pointer)):
                    raise _NativeSecurityFailure
                if not ace_pointer.value:
                    raise _NativeSecurityFailure
                ace = _ACCESS_ALLOWED_ACE.from_address(ace_pointer.value)
                sid = ctypes.c_void_p(
                    ace_pointer.value + _ACCESS_ALLOWED_ACE.sid_start.offset
                )
                if (
                    ace.header.ace_type != self._ACCESS_ALLOWED_ACE_TYPE
                    or ace.header.ace_flags != expected_flags
                    or ace.mask != self._FILE_ALL_ACCESS
                    or not self._IsValidSid(sid)
                ):
                    raise _NativeSecurityFailure
                sid_size = int(self._GetLengthSid(sid))
                if ace.header.ace_size != (
                    ctypes.sizeof(_ACCESS_ALLOWED_ACE)
                    - ctypes.sizeof(_DWORD)
                    + sid_size
                ):
                    raise _NativeSecurityFailure
                if self._EqualSid(sid, self._user_sid):
                    if seen_user:
                        raise _NativeSecurityFailure
                    seen_user = True
                elif self._EqualSid(sid, self._system_sid):
                    if seen_system:
                        raise _NativeSecurityFailure
                    seen_system = True
                else:
                    raise _NativeSecurityFailure
            if not seen_user or not seen_system:
                raise _NativeSecurityFailure
        finally:
            if security_descriptor.value and self._LocalFree(security_descriptor):
                raise _NativeSecurityFailure

    def _require_expected_kind(self, handle: int, *, is_directory: bool) -> None:
        information = _BY_HANDLE_FILE_INFORMATION()
        if not self._GetFileInformationByHandle(handle, ctypes.byref(information)):
            raise _NativeSecurityFailure
        attributes = information.attributes
        if (
            bool(attributes & self._FILE_ATTRIBUTE_DIRECTORY) != is_directory
            or attributes & self._FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise _NativeSecurityFailure

    def _security_access(self) -> int:
        return (
            self._READ_CONTROL
            | self._WRITE_DAC
            | self._WRITE_OWNER
            | self._FILE_READ_ATTRIBUTES
        )

    def _share_all(self) -> int:
        return (
            self._FILE_SHARE_READ | self._FILE_SHARE_WRITE | self._FILE_SHARE_DELETE
        )

    @staticmethod
    def _valid_handle(handle: object) -> bool:
        invalid = ctypes.c_void_p(-1).value
        value = handle.value if isinstance(handle, ctypes.c_void_p) else handle
        return value not in {None, 0, -1, invalid}


_NATIVE_WINDOWS_FILE_SECURITY: WindowsFileSecurity | None = (
    _NativeWindowsFileSecurity() if os.name == "nt" else None
)


def native_windows_file_security() -> WindowsFileSecurity | None:
    """Return the native seam only on Windows."""

    return _NATIVE_WINDOWS_FILE_SECURITY


__all__ = ["WindowsFileSecurity", "native_windows_file_security"]
