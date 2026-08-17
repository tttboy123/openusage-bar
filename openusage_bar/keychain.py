from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
import ctypes
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Callable, Protocol, Sequence

from .bounded_process import BoundedProcessError, run_bounded
from .shared_client_boundary import _record_headless_keychain_get_attempt


SERVICE = "com.lune.openusage-menubar"
INTERNAL_KEYCHAIN_COMMAND = "__keychain-write"
MAX_KEYCHAIN_VALUE_BYTES = 64 * 1024
MAX_KEYCHAIN_PROTOCOL_BYTES = MAX_KEYCHAIN_VALUE_BYTES * 6 + 4_096
_GATEWAY_MUTABLE_ACCOUNT_PATTERN = re.compile(
    r"^(?:openai|anthropic|deepseek|openrouter)\."
    r"account-[0-9a-f]{32}\.gateway-api-key$"
)


class KeychainError(RuntimeError):
    """A sanitized Keychain failure that never contains secret material."""


class KeychainAuthorizationState(str, Enum):
    AUTHORIZED = "authorized"
    MISSING = "missing"
    DENIED = "denied"


class KeychainAPI(Protocol):
    def get(self, query: dict[str, str]) -> bytes | None: ...
    def update(self, query: dict[str, str], value: bytes) -> bool: ...
    def add(self, query: dict[str, str], value: bytes) -> None: ...
    def delete(self, query: dict[str, str]) -> None: ...


_ACCESSIBILITY_UPGRADE_ATTEMPTED: set[tuple[str, str]] = set()


class SecurityFrameworkAPI:
    def __init__(self, *, keychain_path: str | None = None) -> None:
        import Security

        self.security = Security
        self.keychain = None
        if keychain_path is not None:
            if (
                type(keychain_path) is not str
                or not os.path.isabs(keychain_path)
                or not keychain_path
                or "\x00" in keychain_path
            ):
                raise KeychainError("Keychain open failed")
            try:
                status, keychain = Security.SecKeychainOpen(
                    os.fsencode(keychain_path),
                    None,
                )
            except Exception:
                raise KeychainError("Keychain open failed") from None
            if status != Security.errSecSuccess or keychain is None:
                raise KeychainError("Keychain open failed")
            self.keychain = keychain

    def _native_query(self, query: dict[str, str]) -> dict:
        security = self.security
        return {
            security.kSecClass: security.kSecClassGenericPassword,
            security.kSecAttrService: query["service"],
            security.kSecAttrAccount: query["account"],
        }

    def _check(self, status: int, operation: str, allow_missing: bool = False) -> bool:
        if status == self.security.errSecSuccess:
            return True
        if allow_missing and status == self.security.errSecItemNotFound:
            return False
        raise KeychainError(f"Keychain {operation} failed with status {status}")

    def _upgrade_accessibility(self, query: dict[str, str]) -> None:
        # Items written by older builds default to WhenUnlocked and re-prompt
        # whenever the login keychain auto-locks. Best-effort migrate them to
        # AfterFirstUnlockThisDeviceOnly on the first successful read of this
        # process; failures (locked keychain, entitlement) are ignored.
        key = (query.get("service", ""), query.get("account", ""))
        if key in _ACCESSIBILITY_UPGRADE_ATTEMPTED:
            return
        _ACCESSIBILITY_UPGRADE_ATTEMPTED.add(key)
        try:
            self.security.SecItemUpdate(
                self._native_query(query),
                {
                    self.security.kSecAttrAccessible: (
                        self.security.kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
                    )
                },
            )
        except Exception:
            pass

    def get(self, query: dict[str, str]) -> bytes | None:
        if self.keychain is not None:
            value, _ = self._find_explicit(query)
            return value
        native = self._native_query(query)
        native[self.security.kSecReturnData] = True
        native[self.security.kSecMatchLimit] = self.security.kSecMatchLimitOne
        status, result = self.security.SecItemCopyMatching(native, None)
        if not self._check(status, "read", allow_missing=True):
            return None
        self._upgrade_accessibility(query)
        return bytes(result)

    def update(self, query: dict[str, str], value: bytes) -> bool:
        if self.keychain is not None:
            _, item = self._find_explicit(query)
            if item is None:
                return False
            status = self.security.SecItemUpdate(
                {self.security.kSecValueRef: item},
                {self.security.kSecValueData: value},
            )
            return self._check(status, "update")
        status = self.security.SecItemUpdate(
            self._native_query(query), {self.security.kSecValueData: value}
        )
        return self._check(status, "update", allow_missing=True)

    def add(self, query: dict[str, str], value: bytes) -> None:
        if self.keychain is not None:
            service, account = self._explicit_names(query)
            status, _ = self.security.SecKeychainAddGenericPassword(
                self.keychain,
                len(service),
                service,
                len(account),
                account,
                len(value),
                value,
                None,
            )
            self._check(status, "add")
            return
        native = self._native_query(query)
        native[self.security.kSecValueData] = value
        # Readable without unlocking the login keychain after first device
        # unlock; otherwise every collector refresh can trigger a "enter login
        # keychain password" prompt when the keychain has auto-locked.
        native[self.security.kSecAttrAccessible] = (
            self.security.kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        )
        status, _ = self.security.SecItemAdd(native, None)
        self._check(status, "add")

    def delete(self, query: dict[str, str]) -> None:
        if self.keychain is not None:
            _, item = self._find_explicit(query)
            if item is None:
                return
            status = self.security.SecKeychainItemDelete(item)
            self._check(status, "delete")
            return
        status = self.security.SecItemDelete(self._native_query(query))
        self._check(status, "delete", allow_missing=True)

    @staticmethod
    def _explicit_names(query: dict[str, str]) -> tuple[bytes, bytes]:
        try:
            return query["service"].encode("utf-8"), query["account"].encode("utf-8")
        except Exception:
            raise KeychainError("Keychain query failed") from None

    def _find_explicit(
        self,
        query: dict[str, str],
    ) -> tuple[bytes | None, object | None]:
        service, account = self._explicit_names(query)
        try:
            status, length, value, item = (
                self.security.SecKeychainFindGenericPassword(
                    self.keychain,
                    len(service),
                    service,
                    len(account),
                    account,
                    None,
                    None,
                    None,
                )
            )
        except Exception:
            raise KeychainError("Keychain read failed") from None
        if status == self.security.errSecItemNotFound:
            return None, None
        self._check(status, "read")
        if (
            type(length) is not int
            or isinstance(length, bool)
            or length < 0
            or type(value) is not bytes
            or len(value) != length
            or item is None
        ):
            raise KeychainError("Keychain read failed")
        return value, item


class MacOSKeychain:
    def __init__(
        self,
        api: KeychainAPI | None = None,
        *,
        keychain_path: str | None = None,
    ) -> None:
        if api is not None and keychain_path is not None:
            raise ValueError("Keychain authority is ambiguous")
        self.api = (
            api
            if api is not None
            else SecurityFrameworkAPI(keychain_path=keychain_path)
        )

    @staticmethod
    def _query(account: str) -> dict[str, str]:
        if not account:
            raise ValueError("Keychain account must not be empty")
        return {"service": SERVICE, "account": account}

    def get(self, account: str) -> str | None:
        value = self.api.get(self._query(account))
        if value is None:
            return None
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise KeychainError("Keychain value is not valid UTF-8") from error

    def set(self, account: str, secret: str) -> None:
        if not secret:
            raise ValueError("Secret must not be empty")
        query = self._query(account)
        value = secret.encode("utf-8")
        if not self.api.update(query, value):
            self.api.add(query, value)

    def delete(self, account: str) -> None:
        self.api.delete(self._query(account))


class InteractiveKeychainAuthorizer:
    """Prompt for foreground Keychain access without exposing the value."""

    def __init__(
        self,
        *,
        timeout_seconds: int = 90,
        security_executable: str = "/usr/bin/security",
        runner: Callable = run_bounded,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= 120
        ):
            raise ValueError(
                "Interactive Keychain timeout must be between 1 and 120 seconds"
            )
        if (
            not isinstance(security_executable, str)
            or not security_executable.startswith("/")
            or "\x00" in security_executable
        ):
            raise ValueError("Security executable must be an absolute path")
        self.timeout_seconds = timeout_seconds
        self.security_executable = security_executable
        self.runner = runner

    def authorize(
        self,
        *,
        service: str,
        account: str | None,
    ) -> KeychainAuthorizationState:
        if not _valid_keychain_identifier(service):
            raise ValueError("Keychain service is invalid")
        if account is not None and not _valid_account(account):
            raise ValueError("Keychain account is invalid")
        command = [
            self.security_executable,
            "find-generic-password",
            "-s",
            service,
        ]
        if account is not None:
            command.extend(["-a", account])
        command.append("-w")
        try:
            completed = self.runner(
                command,
                shell=False,
                stdout=subprocess.DEVNULL,
                stdout_limit=0,
                stderr_limit=0,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
                env=_keychain_environment(),
            )
        except (BoundedProcessError, OSError, ValueError):
            return KeychainAuthorizationState.DENIED
        if completed.returncode == 0:
            return KeychainAuthorizationState.AUTHORIZED
        if completed.returncode == 44:
            return KeychainAuthorizationState.MISSING
        return KeychainAuthorizationState.DENIED


class BoundedReadOnlyKeychain:
    """Read headless credentials without an unbounded Security-framework prompt."""

    def __init__(
        self,
        timeout_seconds: int = 5,
        security_executable: str = "/usr/bin/security",
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= 30
        ):
            raise ValueError("Keychain timeout must be between 1 and 30 seconds")
        if not security_executable.startswith("/") or "\x00" in security_executable:
            raise ValueError("Security executable must be an absolute path")
        self.timeout_seconds = timeout_seconds
        self.security_executable = security_executable
        self.last_read_bytes = 0
        self.last_process_alive = False

    @staticmethod
    def _kill_and_reap(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                try:
                    process.kill()
                except OSError:
                    pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            process.wait()

    def get(self, account: str) -> str | None:
        if not _valid_account(account):
            return None
        environment = _keychain_environment()
        process: subprocess.Popen[bytes] | None = None
        output = bytearray()
        try:
            process = subprocess.Popen(
                [
                    self.security_executable, "find-generic-password",
                    "-s", SERVICE, "-a", account, "-w",
                ],
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                start_new_session=True,
            )
            assert process.stdout is not None
            deadline = time.monotonic() + self.timeout_seconds
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        self._kill_and_reap(process)
                        return None
                    chunk = os.read(
                        process.stdout.fileno(),
                        min(8192, MAX_KEYCHAIN_VALUE_BYTES + 1 - len(output)),
                    )
                    if not chunk:
                        break
                    output.extend(chunk)
                    self.last_read_bytes = len(output)
                    if len(output) > MAX_KEYCHAIN_VALUE_BYTES:
                        self._kill_and_reap(process)
                        return None
            remaining = max(0.0, deadline - time.monotonic())
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                self._kill_and_reap(process)
                return None
        except (OSError, ValueError):
            if process is not None:
                self._kill_and_reap(process)
            return None
        finally:
            self.last_process_alive = bool(
                process is not None and process.poll() is None
            )
            if process is not None and process.stdout is not None:
                process.stdout.close()
        if returncode != 0:
            return None
        try:
            value = bytes(output).decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            return None
        return value or None

    def set(self, _account: str, _secret: str) -> None:
        raise KeychainError("Headless Keychain access is read-only")


def _valid_account(account: object) -> bool:
    return (
        isinstance(account, str)
        and 0 < len(account.encode("utf-8")) <= 512
        and not any(character in account for character in "\x00\r\n")
    )


def _valid_keychain_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value.encode("utf-8")) <= 512
        and not any(character in value for character in "\x00\r\n")
    )


def _mutable_keychain_account(value: object) -> bool:
    if not _valid_account(value):
        return False
    assert isinstance(value, str)
    if value.endswith(".oasis-token"):
        return True
    return _GATEWAY_MUTABLE_ACCOUNT_PATTERN.fullmatch(value) is not None


def _write_protocol_response(output_stream: BinaryIO, payload: dict) -> None:
    output_stream.write(
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    output_stream.flush()


def run_native_keychain_write(
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    *,
    keychain: MacOSKeychain | None = None,
) -> int:
    """Persist one Step Plan session rotation over a private bounded pipe."""
    try:
        raw = input_stream.read(MAX_KEYCHAIN_PROTOCOL_BYTES + 1)
        if len(raw) > MAX_KEYCHAIN_PROTOCOL_BYTES:
            raise ValueError("invalid request")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("invalid request")
        action = payload.get("action")
        account = payload.get("account")
        if not _mutable_keychain_account(account):
            raise ValueError("invalid request")
        resolved = keychain or MacOSKeychain()
        if action == "set" and set(payload) == {
            "version", "action", "account", "secret",
        }:
            secret = payload.get("secret")
            if (
                not isinstance(secret, str)
                or not secret
                or len(secret.encode("utf-8")) > MAX_KEYCHAIN_VALUE_BYTES
            ):
                raise ValueError("invalid request")
            resolved.set(account, secret)
            _write_protocol_response(
                output_stream,
                {"version": 1, "ok": True},
            )
            return 0
        if action == "delete" and set(payload) == {
            "version", "action", "account",
        }:
            resolved.delete(account)
            _write_protocol_response(
                output_stream,
                {"version": 1, "ok": True},
            )
            return 0
        raise ValueError("invalid request")
    except Exception:
        _write_protocol_response(
            output_stream,
            {"version": 1, "ok": False},
        )
        return 1


def _default_keychain_helper_command() -> tuple[str, ...]:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        if (
            not executable.is_absolute()
            or "\x00" in str(executable)
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise KeychainError("Keychain helper is unavailable")
        return (str(executable),)
    entrypoint = Path(__file__).resolve().parent.parent / "openusage_settings.py"
    return (sys.executable, str(entrypoint))


def _keychain_environment() -> dict[str, str]:
    environment = {"PATH": "/usr/bin:/bin"}
    for name in ("HOME", "USER", "LOGNAME", "TMPDIR"):
        value = os.environ.get(name)
        if value and "\x00" not in value:
            environment[name] = value
    return environment


class BoundedMacOSKeychain:
    """Bound reads through security(1) and one session write through a helper."""

    def __init__(
        self,
        *,
        timeout_seconds: int = 5,
        helper_command: Sequence[str] | None = None,
        reader: BoundedReadOnlyKeychain | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= 30
        ):
            raise ValueError("Keychain timeout must be between 1 and 30 seconds")
        base = tuple(helper_command or _default_keychain_helper_command())
        if (
            not base
            or any(
                not isinstance(part, str) or not part or "\x00" in part
                for part in base
            )
            or not Path(base[0]).is_absolute()
        ):
            raise ValueError("Keychain helper command is invalid")
        self.timeout_seconds = timeout_seconds
        self.command = (*base, INTERNAL_KEYCHAIN_COMMAND)
        self.reader = reader or BoundedReadOnlyKeychain(
            timeout_seconds=timeout_seconds
        )
        # account -> (value, monotonic timestamp). A non-None value is cached
        # until set/delete; a None (unavailable) result is re-validated after
        # the negative TTL so a transiently locked keychain is retried without
        # re-prompting on every refresh.
        self._cache: dict[str, tuple[str | None, float]] = {}
        self._negative_cache_seconds = 60

    def _request(self, payload: dict) -> dict:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_KEYCHAIN_PROTOCOL_BYTES:
            raise KeychainError("Keychain request is too large")
        try:
            completed = run_bounded(
                self.command,
                timeout=self.timeout_seconds,
                stdout_limit=MAX_KEYCHAIN_PROTOCOL_BYTES,
                stderr_limit=0,
                input_data=encoded,
                env=_keychain_environment(),
            )
            if completed.returncode != 0:
                raise KeychainError("Keychain helper failed")
            response = json.loads(completed.stdout)
            if not isinstance(response, dict) or response.get("version") != 1:
                raise KeychainError("Keychain helper failed")
            return response
        except (
            BoundedProcessError,
            json.JSONDecodeError,
            UnicodeError,
            OSError,
            ValueError,
        ) as error:
            raise KeychainError("Keychain helper failed") from error

    def get(self, account: str) -> str | None:
        now = time.monotonic()
        cached = self._cache.get(account)
        if cached is not None:
            value, cached_at = cached
            if value is not None or now - cached_at < self._negative_cache_seconds:
                return value
        try:
            value = self.reader.get(account)
        except (KeychainError, OSError, ValueError):
            return None
        self._cache[account] = (value, now)
        return value

    def set(self, account: str, secret: str) -> None:
        if not _mutable_keychain_account(account):
            raise KeychainError("Keychain account is invalid")
        if (
            not isinstance(secret, str)
            or not secret
            or len(secret.encode("utf-8")) > MAX_KEYCHAIN_VALUE_BYTES
        ):
            raise KeychainError("Keychain value is invalid")
        response = self._request({
            "version": 1,
            "action": "set",
            "account": account,
            "secret": secret,
        })
        if set(response) != {"version", "ok"} or response.get("ok") is not True:
            raise KeychainError("Keychain helper failed")
        self._cache[account] = (secret, time.monotonic())

    def delete(self, account: str) -> None:
        if not _mutable_keychain_account(account):
            raise KeychainError("Keychain account is invalid")
        response = self._request({
            "version": 1,
            "action": "delete",
            "account": account,
        })
        if set(response) != {"version", "ok"} or response.get("ok") is not True:
            raise KeychainError("Keychain helper failed")
        self._cache.pop(account, None)


class UnsupportedPlatformKeychainError(KeychainError):
    """Raised when the current platform has no usable credential backend."""


class HeadlessKeychain:
    """Keychain facade over any KeychainAPI for headless collectors.

    Matches the ``get`` / ``set`` / ``delete`` surface used by provider
    adapters and the aggregation pipeline without macOS-specific subprocesses.
    """

    def __init__(self, api: KeychainAPI) -> None:
        if api is None:
            raise ValueError("Keychain API is required")
        self._api = api

    @staticmethod
    def _query(account: str) -> dict[str, str]:
        if not account:
            raise ValueError("Keychain account must not be empty")
        return {"service": SERVICE, "account": account}

    def get(self, account: str) -> str | None:
        query = self._query(account)
        _record_headless_keychain_get_attempt()
        value = self._api.get(query)
        if value is None:
            return None
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise KeychainError("Keychain value is not valid UTF-8") from error

    def set(self, account: str, secret: str) -> None:
        if not secret:
            raise ValueError("Secret must not be empty")
        query = self._query(account)
        value = secret.encode("utf-8")
        if not self._api.update(query, value):
            self._api.add(query, value)

    def delete(self, account: str) -> None:
        self._api.delete(self._query(account))


def _win_target(query: dict[str, str]) -> str:
    service = query.get("service") or SERVICE
    account = query.get("account") or ""
    if not _valid_account(account):
        raise ValueError("Credential Manager account must not be empty")
    return f"{service}\\{account}"


class _WinCredential(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", ctypes.c_uint64),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


class _WinCredentialNative:
    """Minimal advapi32 Credential Manager binding (Windows only)."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise UnsupportedPlatformKeychainError(
                "Windows Credential Manager binding requires Windows"
            )
        import ctypes as _ctypes
        from ctypes import wintypes

        self._ctypes = _ctypes
        self._wintypes = wintypes
        advapi32 = _ctypes.WinDLL("advapi32", use_last_error=True)
        self._cred_read = advapi32.CredReadW
        self._cred_read.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            _ctypes.POINTER(_ctypes.POINTER(_WinCredential)),
        ]
        self._cred_read.restype = wintypes.BOOL
        self._cred_write = advapi32.CredWriteW
        self._cred_write.argtypes = [_ctypes.POINTER(_WinCredential), wintypes.DWORD]
        self._cred_write.restype = wintypes.BOOL
        self._cred_delete = advapi32.CredDeleteW
        self._cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._cred_delete.restype = wintypes.BOOL
        self._cred_free = advapi32.CredFree
        self._cred_free.argtypes = [_ctypes.c_void_p]
        self._cred_free.restype = None
        self._get_last_error = _ctypes.get_last_error

    def read(self, target: str) -> bytes | None:
        credential = self._ctypes.POINTER(_WinCredential)()
        if not self._cred_read(
            target,
            self.CRED_TYPE_GENERIC,
            0,
            self._ctypes.byref(credential),
        ):
            if self._get_last_error() == self.ERROR_NOT_FOUND:
                return None
            raise KeychainError("Credential Manager read failed")
        try:
            size = int(credential.contents.CredentialBlobSize)
            if size <= 0 or credential.contents.CredentialBlob is None:
                return b""
            return bytes(
                self._ctypes.string_at(credential.contents.CredentialBlob, size)
            )
        finally:
            self._cred_free(credential)

    def write(self, target: str, value: bytes) -> None:
        if len(value) > MAX_KEYCHAIN_VALUE_BYTES:
            raise KeychainError("Credential value is too large")
        blob = self._ctypes.create_string_buffer(value)
        credential = _WinCredential()
        credential.Type = self.CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.CredentialBlobSize = len(value)
        credential.CredentialBlob = self._ctypes.cast(blob, self._ctypes.POINTER(self._ctypes.c_ubyte))
        credential.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = None
        if not self._cred_write(self._ctypes.byref(credential), 0):
            raise KeychainError("Credential Manager write failed")

    def delete(self, target: str) -> None:
        if not self._cred_delete(target, self.CRED_TYPE_GENERIC, 0):
            if self._get_last_error() == self.ERROR_NOT_FOUND:
                return
            raise KeychainError("Credential Manager delete failed")


class WindowsCredentialManagerAPI(KeychainAPI):
    """Generic Credential Manager backend (win32) implementing KeychainAPI."""

    def __init__(self, *, native: object | None = None) -> None:
        if sys.platform != "win32" and native is None:
            raise UnsupportedPlatformKeychainError(
                "Windows Credential Manager backend requires Windows"
            )
        self._native = native if native is not None else _WinCredentialNative()

    def get(self, query: dict[str, str]) -> bytes | None:
        return self._native.read(_win_target(query))

    def update(self, query: dict[str, str], value: bytes) -> bool:
        target = _win_target(query)
        existed = self._native.read(target) is not None
        self._native.write(target, value)
        return existed

    def add(self, query: dict[str, str], value: bytes) -> None:
        self._native.write(_win_target(query), value)

    def delete(self, query: dict[str, str]) -> None:
        self._native.delete(_win_target(query))


class _SecretServiceBackend:
    """org.freedesktop.secrets backend through the optional secretstorage module."""

    def __init__(self) -> None:
        self._secretstorage = None
        self._bus = None

    def _connect(self):
        if self._secretstorage is not None:
            return self._bus
        try:
            import secretstorage
        except ImportError as error:
            raise KeychainError(
                "Linux Secret Service requires the optional 'secretstorage' dependency"
            ) from error
        self._secretstorage = secretstorage
        self._bus = secretstorage.dbus_init()
        return self._bus

    def _collection(self):
        bus = self._connect()
        return self._secretstorage.get_default_collection(bus)

    @staticmethod
    def _attributes(query: dict[str, str]) -> dict[str, str]:
        account = query.get("account") or ""
        if not _valid_account(account):
            raise ValueError("Secret Service account must not be empty")
        return {"application": SERVICE, "account": account}

    def get(self, query: dict[str, str]) -> bytes | None:
        collection = self._collection()
        items = collection.search_items(self._attributes(query))
        for item in items:
            if getattr(item, "is_locked", False):
                item.unlock()
            secret = item.get_secret()
            if isinstance(secret, str):
                return secret.encode("utf-8")
            return bytes(secret)
        return None

    def set(self, query: dict[str, str], value: bytes) -> None:
        if len(value) > MAX_KEYCHAIN_VALUE_BYTES:
            raise KeychainError("Secret Service value is too large")
        account = query.get("account") or ""
        if not _valid_account(account):
            raise ValueError("Secret Service account must not be empty")
        self._collection().create_item(
            f"openusage-bar:{account}",
            self._attributes(query),
            value.decode("utf-8"),
            replace=True,
        )

    def delete(self, query: dict[str, str]) -> None:
        for item in self._collection().search_items(self._attributes(query)):
            item.delete()


class LinuxSecretServiceAPI(KeychainAPI):
    """Secret Service backend (linux) implementing KeychainAPI."""

    def __init__(self, *, backend: object | None = None) -> None:
        if sys.platform != "linux" and backend is None:
            raise UnsupportedPlatformKeychainError(
                "Linux Secret Service backend requires Linux"
            )
        self._backend = backend if backend is not None else _SecretServiceBackend()

    def get(self, query: dict[str, str]) -> bytes | None:
        value = self._backend.get(query)
        if value is None:
            return None
        if len(value) > MAX_KEYCHAIN_VALUE_BYTES:
            raise KeychainError("Secret Service value is too large")
        return value

    def update(self, query: dict[str, str], value: bytes) -> bool:
        existed = self._backend.get(query) is not None
        self._backend.set(query, value)
        return existed

    def add(self, query: dict[str, str], value: bytes) -> None:
        self._backend.set(query, value)

    def delete(self, query: dict[str, str]) -> None:
        self._backend.delete(query)


def default_keychain_api() -> KeychainAPI:
    """Return the platform-native KeychainAPI implementation."""
    if sys.platform == "darwin":
        return SecurityFrameworkAPI()
    if sys.platform == "win32":
        return WindowsCredentialManagerAPI()
    if sys.platform.startswith("linux"):
        return LinuxSecretServiceAPI()
    raise UnsupportedPlatformKeychainError(
        f"no credential backend for platform {sys.platform}"
    )


def default_keychain() -> object:
    """Return the bounded headless keychain for the current platform."""
    if sys.platform == "darwin":
        return BoundedMacOSKeychain()
    return HeadlessKeychain(default_keychain_api())
