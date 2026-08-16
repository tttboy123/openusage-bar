"""Private account references and renderer-safe account projections."""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

from ..config import ID_PATTERN
from ..keychain import default_keychain


_MAX_ALIAS_LENGTH = 80
_MAX_CREDENTIAL_ACCOUNT_LENGTH = 256


class AccountState(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DISABLED = "disabled"
    COOLDOWN = "cooldown"
    BACKEND_UNAVAILABLE = "backend_unavailable"


class AccountCredentialError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("account credential cleanup failed")

    def __repr__(self) -> str:
        return "AccountCredentialError()"


def _valid_identifier(value: object) -> bool:
    return type(value) is str and ID_PATTERN.fullmatch(value) is not None


def _valid_alias(value: object) -> bool:
    return (
        type(value) is str
        and value == value.strip()
        and 1 <= len(value) <= _MAX_ALIAS_LENGTH
        and all(
            not unicodedata.category(character).startswith("C")
            for character in value
        )
    )


@dataclass(frozen=True)
class ProviderAccountRef:
    """One stable local account whose credential lookup name stays private."""

    provider_id: str
    account_id: str = field(repr=False)
    alias: str
    credential_account: str = field(repr=False)

    def __post_init__(self) -> None:
        if not _valid_identifier(self.provider_id):
            raise ValueError("provider_id must use the stable identifier grammar")
        if not _valid_identifier(self.account_id):
            raise ValueError("account_id must use the stable identifier grammar")
        if not _valid_alias(self.alias):
            raise ValueError("account alias is invalid")
        if (
            not _valid_identifier(self.credential_account)
            or len(self.credential_account) > _MAX_CREDENTIAL_ACCOUNT_LENGTH
            or self.credential_account
            != f"{self.provider_id}.{self.account_id}.gateway-api-key"
        ):
            raise ValueError("credential account is invalid")

    @property
    def display_id(self) -> str:
        digest = hashlib.sha256(
            f"{self.provider_id}\0{self.account_id}".encode("utf-8")
        ).hexdigest()
        return f"acct_{digest[:12]}"

    def to_public_dict(
        self,
        *,
        state: AccountState = AccountState.UNKNOWN,
    ) -> dict[str, object]:
        if not isinstance(state, AccountState):
            raise TypeError("account state is invalid")
        return {
            "alias": self.alias,
            "displayId": self.display_id,
            "providerId": self.provider_id,
            "state": state.value,
        }


def delete_provider_account_credential(
    account: ProviderAccountRef,
    *,
    keychain: object | None = None,
) -> None:
    """Delete one account credential without reading or returning its value."""

    if type(account) is not ProviderAccountRef:
        raise TypeError("provider account is invalid")
    try:
        boundary = keychain if keychain is not None else default_keychain()
        delete = getattr(boundary, "delete", None)
        if not callable(delete):
            raise TypeError
        delete(account.credential_account)
    except Exception:
        raise AccountCredentialError() from None


__all__ = [
    "AccountCredentialError",
    "AccountState",
    "ProviderAccountRef",
    "delete_provider_account_credential",
]
