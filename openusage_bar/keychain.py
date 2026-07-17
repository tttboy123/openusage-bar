from __future__ import annotations

from typing import Protocol


SERVICE = "com.lune.openusage-menubar"


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
