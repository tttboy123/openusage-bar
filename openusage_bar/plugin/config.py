"""Private principal token registry for the independent Plugin listener."""

from __future__ import annotations

import hmac
import os
import secrets
import stat
import time
from pathlib import Path

from ..windows_file_security import native_windows_file_security
from .contracts import PRINCIPALS


_WINDOWS_FILE_SECURITY = native_windows_file_security()
_MAX_PATH = 4096
_TOKEN_BYTES = 48


def _validate_state_dir(value: object) -> Path:
    if not isinstance(value, Path):
        raise ValueError("invalid Plugin state directory")
    raw = os.fspath(value)
    if not value.is_absolute() or ".." in value.parts or not raw or len(raw) > _MAX_PATH or "\0" in raw:
        raise ValueError("invalid Plugin state directory")
    value.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = value.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError("unsafe Plugin state directory")
    if os.name == "nt":
        if _WINDOWS_FILE_SECURITY is None:
            raise ValueError("Windows Plugin token security unavailable")
        _WINDOWS_FILE_SECURITY.harden_directory(value)
    else:
        geteuid = getattr(os, "geteuid", None)
        if callable(geteuid) and int(metadata.st_uid) != int(geteuid()):
            raise ValueError("unsafe Plugin state directory")
        try:
            os.chmod(value, 0o700, follow_symlinks=False)
        except (NotImplementedError, TypeError):
            os.chmod(value, 0o700)
    return value


def _load_or_create_token(path: Path) -> str:
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode) or int(existing.st_nlink) != 1
    ):
        raise ValueError("unsafe Plugin token file")
    created = False
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
            0o600,
        )
        created = True
    except FileExistsError:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
        )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or int(opened.st_nlink) != 1:
            raise ValueError("unsafe Plugin token file")
        if os.name == "nt":
            if _WINDOWS_FILE_SECURITY is None:
                raise ValueError("Windows Plugin token security unavailable")
            _WINDOWS_FILE_SECURITY.harden_file(descriptor)
            _WINDOWS_FILE_SECURITY.verify_file(descriptor)
        elif hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        if created:
            token = secrets.token_urlsafe(_TOKEN_BYTES).encode("ascii")
            written = os.write(descriptor, token)
            if written != len(token):
                raise OSError("short Plugin token write")
            os.fsync(descriptor)
            raw = token
        else:
            raw = b""
            deadline = time.monotonic() + 0.5
            while not raw:
                os.lseek(descriptor, 0, os.SEEK_SET)
                raw = os.read(descriptor, 129)
                if raw or time.monotonic() >= deadline:
                    break
                time.sleep(0.025)
        after = path.lstat()
        if (
            not stat.S_ISREG(after.st_mode) or int(after.st_nlink) != 1
            or (int(after.st_dev), int(after.st_ino))
            != (int(opened.st_dev), int(opened.st_ino))
        ):
            raise ValueError("unsafe Plugin token file")
    finally:
        os.close(descriptor)
    try:
        decoded = raw.decode("ascii", "strict")
    except UnicodeError:
        raise ValueError("invalid Plugin token file") from None
    token = decoded
    if not 43 <= len(token) <= 128 or any(not 0x21 <= ord(character) <= 0x7E for character in token):
        raise ValueError("invalid Plugin token file")
    return token


def read_private_token(path: Path) -> str:
    """Read one existing internal-service token without following aliases."""
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise ValueError("invalid private token path")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or int(opened.st_nlink) != 1:
            raise ValueError("unsafe private token file")
        if os.name == "nt":
            if _WINDOWS_FILE_SECURITY is None:
                raise ValueError("Windows token security unavailable")
            _WINDOWS_FILE_SECURITY.verify_file(descriptor)
        else:
            geteuid = getattr(os, "geteuid", None)
            if stat.S_IMODE(opened.st_mode) != 0o600 or (
                callable(geteuid) and int(opened.st_uid) != int(geteuid())
            ):
                raise ValueError("unsafe private token file")
        raw = os.read(descriptor, 1025)
        after = path.lstat()
        if (
            not stat.S_ISREG(after.st_mode) or int(after.st_nlink) != 1
            or (int(after.st_dev), int(after.st_ino))
            != (int(opened.st_dev), int(opened.st_ino))
        ):
            raise ValueError("unsafe private token file")
    finally:
        os.close(descriptor)
    try:
        decoded = raw.decode("ascii", "strict")
    except UnicodeError:
        raise ValueError("invalid private token file") from None
    if not decoded.endswith("\n") or decoded.count("\n") != 1:
        raise ValueError("invalid private token file")
    token = decoded[:-1]
    if not 1 <= len(token) <= 256 or any(character.isspace() for character in token):
        raise ValueError("invalid private token file")
    return token


class PluginPrincipalRegistry:
    def __init__(self, state_dir: Path, tokens: dict[str, str]) -> None:
        self._state_dir = state_dir
        self._tokens = dict(tokens)

    @classmethod
    def load_or_create(cls, state_dir: Path) -> "PluginPrincipalRegistry":
        directory = _validate_state_dir(state_dir)
        tokens = {
            principal: _load_or_create_token(directory / f"{principal}.token")
            for principal in PRINCIPALS
        }
        if len(set(tokens.values())) != len(PRINCIPALS):
            raise ValueError("Plugin principal tokens must be distinct")
        return cls(directory, tokens)

    def authenticate(self, token: str) -> str | None:
        if type(token) is not str or not 1 <= len(token) <= 256:
            return None
        matched: str | None = None
        for principal in PRINCIPALS:
            if hmac.compare_digest(token, self._tokens[principal]):
                matched = principal
        return matched

    def token_path(self, principal: str) -> Path:
        if principal not in PRINCIPALS:
            raise ValueError("invalid Plugin principal")
        return self._state_dir / f"{principal}.token"

    @property
    def configured_external_principals(self) -> tuple[str, ...]:
        return PRINCIPALS[:3]
