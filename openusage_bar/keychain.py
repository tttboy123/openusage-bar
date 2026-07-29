from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO, Protocol, Sequence

from .bounded_process import BoundedProcessError, run_bounded


SERVICE = "com.lune.openusage-menubar"
INTERNAL_KEYCHAIN_COMMAND = "__keychain-write"
MAX_KEYCHAIN_VALUE_BYTES = 64 * 1024
MAX_KEYCHAIN_PROTOCOL_BYTES = MAX_KEYCHAIN_VALUE_BYTES * 6 + 4_096


class KeychainError(RuntimeError):
    """A sanitized Keychain failure that never contains secret material."""


class KeychainAPI(Protocol):
    def get(self, query: dict[str, str]) -> bytes | None: ...
    def update(self, query: dict[str, str], value: bytes) -> bool: ...
    def add(self, query: dict[str, str], value: bytes) -> None: ...
    def delete(self, query: dict[str, str]) -> None: ...


class SecurityFrameworkAPI:
    def __init__(self) -> None:
        import Security

        self.security = Security

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

    def get(self, query: dict[str, str]) -> bytes | None:
        native = self._native_query(query)
        native[self.security.kSecReturnData] = True
        native[self.security.kSecMatchLimit] = self.security.kSecMatchLimitOne
        status, result = self.security.SecItemCopyMatching(native, None)
        if not self._check(status, "read", allow_missing=True):
            return None
        return bytes(result)

    def update(self, query: dict[str, str], value: bytes) -> bool:
        status = self.security.SecItemUpdate(
            self._native_query(query), {self.security.kSecValueData: value}
        )
        return self._check(status, "update", allow_missing=True)

    def add(self, query: dict[str, str], value: bytes) -> None:
        native = self._native_query(query)
        native[self.security.kSecValueData] = value
        status, _ = self.security.SecItemAdd(native, None)
        self._check(status, "add")

    def delete(self, query: dict[str, str]) -> None:
        status = self.security.SecItemDelete(self._native_query(query))
        self._check(status, "delete", allow_missing=True)


class MacOSKeychain:
    def __init__(self, api: KeychainAPI | None = None) -> None:
        self.api = api or SecurityFrameworkAPI()

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
        if not _valid_account(account):
            raise ValueError("invalid request")
        resolved = keychain or MacOSKeychain()
        if action == "set" and set(payload) == {
            "version", "action", "account", "secret",
        }:
            if not account.endswith(".oasis-token"):
                raise ValueError("invalid request")
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
        try:
            return self.reader.get(account)
        except (KeychainError, OSError, ValueError):
            return None

    def set(self, account: str, secret: str) -> None:
        if not _valid_account(account) or not account.endswith(".oasis-token"):
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
