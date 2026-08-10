"""Secret-free configuration for the opt-in local Gateway."""

from __future__ import annotations

import json
import os
import secrets
import stat
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..windows_file_security import native_windows_file_security
from .accounts import ProviderAccountRef
from .contracts import GatewayMode
from .pools import AccountPool, PoolMember, PoolStrategy


_MAX_CONFIG_BYTES = 64 * 1024
_LOCK_TIMEOUT_SECONDS = 0.5
_LOCK_RETRY_SECONDS = 0.025
DEFAULT_GATEWAY_CONFIG_PATH = Path.home() / ".config" / "openusage-bar" / "gateway.json"
_WINDOWS_FILE_SECURITY = native_windows_file_security()
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_CONFIG_FIELDS = {
    "enabled",
    "mode",
    "host",
    "port",
    "proxy_enabled",
    "cache_enabled",
    "accounts",
    "account_pools",
}
_SECRET_FIELDS = {"apikey", "secret", "token", "password", "cookie"}


@dataclass(frozen=True)
class GatewayConfig:
    enabled: bool = False
    mode: GatewayMode = GatewayMode.OBSERVE
    host: str = "127.0.0.1"
    port: int = 17823
    proxy_enabled: bool = False
    cache_enabled: bool = False
    accounts: tuple[ProviderAccountRef, ...] = ()
    account_pools: tuple[AccountPool, ...] = ()

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("Gateway enabled must be a boolean.")
        if not isinstance(self.mode, GatewayMode):
            raise ValueError("Gateway mode is invalid.")
        if self.host != "127.0.0.1":
            raise ValueError("Gateway host must be 127.0.0.1.")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Gateway port is invalid.")
        if type(self.proxy_enabled) is not bool:
            raise ValueError("Gateway proxy setting must be a boolean.")
        if type(self.cache_enabled) is not bool:
            raise ValueError("Gateway cache setting must be a boolean.")
        if type(self.accounts) is not tuple or any(
            type(account) is not ProviderAccountRef for account in self.accounts
        ):
            raise TypeError("Gateway accounts are invalid.")
        if type(self.account_pools) is not tuple or any(
            type(pool) is not AccountPool for pool in self.account_pools
        ):
            raise TypeError("Gateway account pools are invalid.")
        account_ids = tuple(account.account_id for account in self.accounts)
        if len(set(account_ids)) != len(account_ids):
            raise ValueError("Gateway account IDs must be unique.")
        pool_ids = tuple(pool.pool_id for pool in self.account_pools)
        if len(set(pool_ids)) != len(pool_ids):
            raise ValueError("Gateway pool IDs must be unique.")
        known_accounts = set(account_ids)
        if any(
            member.account_id not in known_accounts
            for pool in self.account_pools
            for member in pool.members
        ):
            raise ValueError("Gateway pool references an unknown account.")
        if self.proxy_enabled and (
            not self.enabled or self.mode is not GatewayMode.GATEWAY
        ):
            raise ValueError(
                "Gateway proxy requires gateway mode with enabled configuration."
            )


class GatewayConfigLockError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Gateway configuration lock is unavailable.")


def _normalized_field_name(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _reject_secret_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if _normalized_field_name(key) in _SECRET_FIELDS:
                raise ValueError(
                    "Secret field is not permitted in Gateway configuration."
                )
            _reject_secret_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_secret_fields(nested)


def _gateway_config_payload(config: GatewayConfig) -> dict[str, object]:
    if type(config) is not GatewayConfig:
        raise ValueError("Gateway configuration is invalid.")
    payload: dict[str, object] = {
        "enabled": config.enabled,
        "mode": config.mode.value,
        "host": config.host,
        "port": config.port,
        "proxy_enabled": config.proxy_enabled,
        "cache_enabled": config.cache_enabled,
        "accounts": [
            {
                "provider_id": account.provider_id,
                "account_id": account.account_id,
                "alias": account.alias,
            }
            for account in config.accounts
        ],
        "account_pools": [
            {
                "pool_id": pool.pool_id,
                "revision": pool.revision,
                "strategy": pool.strategy.value,
                "members": [
                    {
                        "account_id": member.account_id,
                        "priority": member.priority,
                        "weight": member.weight,
                    }
                    for member in pool.members
                ],
                "cross_provider_fallback": pool.cross_provider_fallback,
                "cross_model_fallback": pool.cross_model_fallback,
                "cross_region_fallback": pool.cross_region_fallback,
            }
            for pool in config.account_pools
        ],
    }
    _reject_secret_fields(payload)
    load_gateway_config_from_payload(payload)
    return payload


def _prepare_private_parent(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Gateway configuration path is invalid.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise ValueError("Gateway configuration parent is unsafe.")
    if hasattr(os, "getuid"):
        if parent.st_uid != os.getuid():
            raise ValueError("Gateway configuration parent is unsafe.")
        try:
            os.chmod(path.parent, 0o700, follow_symlinks=False)
        except (NotImplementedError, TypeError):
            os.chmod(path.parent, 0o700)
        secured = path.parent.lstat()
        if (
            stat.S_ISLNK(secured.st_mode)
            or not stat.S_ISDIR(secured.st_mode)
            or secured.st_uid != os.getuid()
            or stat.S_IMODE(secured.st_mode) != 0o700
        ):
            raise ValueError("Gateway configuration parent is unsafe.")
    if _WINDOWS_FILE_SECURITY is not None:
        try:
            _WINDOWS_FILE_SECURITY.harden_directory(path.parent)
        except Exception:
            raise ValueError("Gateway configuration parent is unsafe.") from None


def _private_regular(metadata: os.stat_result) -> bool:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        return False
    if hasattr(os, "getuid"):
        return (
            metadata.st_uid == os.getuid()
            and stat.S_IMODE(metadata.st_mode) == 0o600
        )
    return True


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _verify_private_read_metadata(metadata: os.stat_result) -> None:
    if not _private_regular(metadata):
        raise ValueError("Gateway configuration file is unsafe.")


def _harden_private_file(descriptor: int) -> None:
    if _WINDOWS_FILE_SECURITY is not None:
        try:
            _WINDOWS_FILE_SECURITY.harden_file(descriptor)
        except Exception:
            raise ValueError("Gateway configuration file is unsafe.") from None
        return
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_lock_key(path: Path) -> str:
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path.absolute())


def _process_lock_for(path: Path) -> threading.Lock:
    key = _canonical_lock_key(path)
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _acquire_process_lock(lock: threading.Lock, deadline: float) -> None:
    while True:
        if lock.acquire(blocking=False):
            return
        if time.monotonic() >= deadline:
            raise GatewayConfigLockError()
        time.sleep(_LOCK_RETRY_SECONDS)


def _lock_file_path(path: Path) -> Path:
    return path.parent / f".{path.name}.lock"


def _open_private_lock_file(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(_lock_file_path(path), flags, 0o600)
    except OSError:
        raise GatewayConfigLockError() from None
    try:
        _harden_private_file(descriptor)
        metadata = os.fstat(descriptor)
        if not _private_regular(metadata):
            raise GatewayConfigLockError()
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _acquire_file_lock(descriptor: int, deadline: float) -> None:
    if os.name == "nt":
        try:
            import msvcrt
        except ImportError:
            raise GatewayConfigLockError() from None
        while True:
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise GatewayConfigLockError() from None
                time.sleep(_LOCK_RETRY_SECONDS)
    else:
        try:
            import fcntl
        except ImportError:
            raise GatewayConfigLockError() from None
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise GatewayConfigLockError() from None
                time.sleep(_LOCK_RETRY_SECONDS)


def _release_file_lock(descriptor: int) -> None:
    if os.name == "nt":
        try:
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass


def _read_private_config(path: Path) -> bytes | None:
    unsafe = ValueError("Gateway configuration file is unsafe.")
    try:
        existing = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise unsafe from None
    _verify_private_read_metadata(existing)

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError:
            raise unsafe from None

        opened = os.fstat(descriptor)
        if not _same_file(existing, opened):
            raise unsafe
        _verify_private_read_metadata(opened)
        if _WINDOWS_FILE_SECURITY is not None:
            try:
                _WINDOWS_FILE_SECURITY.verify_file(descriptor)
            except Exception:
                raise unsafe from None

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(8192, _MAX_CONFIG_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_CONFIG_BYTES:
                raise ValueError("Gateway configuration is too large.")
        raw = b"".join(chunks)
        after_read = os.fstat(descriptor)
        final = path.lstat()
        if (
            not _same_file(opened, after_read)
            or not _same_file(opened, final)
            or not _private_regular(after_read)
            or not _private_regular(final)
        ):
            raise unsafe
        closing_descriptor = descriptor
        descriptor = None
        try:
            os.close(closing_descriptor)
        except OSError:
            raise unsafe from None
        return raw
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                raise unsafe from None


def load_gateway_config_from_payload(payload: dict[str, object]) -> GatewayConfig:
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _load_gateway_config_from_raw(encoded)


def _read_config(path: Path) -> bytes | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("Gateway configuration could not be read.") from None

    if len(raw) > _MAX_CONFIG_BYTES:
        raise ValueError("Gateway configuration is too large.")
    return raw


def _load_gateway_config_from_raw(raw: bytes) -> GatewayConfig:
    payload = _parse_object(raw)
    try:
        _reject_secret_fields(payload)
    except RecursionError:
        raise ValueError("Gateway configuration is invalid.") from None

    if any(key not in _CONFIG_FIELDS for key in payload):
        raise ValueError("Gateway configuration contains an unknown field.")

    mode_value = payload.get("mode", GatewayMode.OBSERVE.value)
    if not isinstance(mode_value, str):
        raise ValueError("Gateway mode is invalid.")
    try:
        mode = GatewayMode(mode_value)
    except ValueError:
        raise ValueError("Gateway mode is invalid.") from None

    return GatewayConfig(
        enabled=payload.get("enabled", False),
        mode=mode,
        host=payload.get("host", "127.0.0.1"),
        port=payload.get("port", 17823),
        proxy_enabled=payload.get("proxy_enabled", False),
        cache_enabled=payload.get("cache_enabled", False),
        accounts=_parse_accounts(payload.get("accounts", [])),
        account_pools=_parse_account_pools(payload.get("account_pools", [])),
    )


def _parse_object(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Gateway configuration is invalid.") from None
    if not isinstance(payload, dict):
        raise ValueError("Gateway configuration must be a JSON object.")
    return payload


def _parse_accounts(value: object) -> tuple[ProviderAccountRef, ...]:
    if type(value) is not list or len(value) > 128:
        raise ValueError("Gateway accounts are invalid.")
    result: list[ProviderAccountRef] = []
    for item in value:
        if type(item) is not dict or set(item) != {
            "provider_id",
            "account_id",
            "alias",
        }:
            raise ValueError("Gateway account is invalid.")
        try:
            provider_id = item["provider_id"]
            account_id = item["account_id"]
            if type(provider_id) is not str or type(account_id) is not str:
                raise ValueError
            result.append(
                ProviderAccountRef(
                    provider_id=provider_id,
                    account_id=account_id,
                    alias=item["alias"],
                    credential_account=(
                        f"{provider_id}.{account_id}.gateway-api-key"
                    ),
                )
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("Gateway account is invalid.") from None
    return tuple(result)


def _parse_pool_members(value: object) -> tuple[PoolMember, ...]:
    if type(value) is not list:
        raise ValueError("Gateway pool members are invalid.")
    members: list[PoolMember] = []
    for item in value:
        if type(item) is not dict or set(item) != {
            "account_id",
            "priority",
            "weight",
        }:
            raise ValueError("Gateway pool member is invalid.")
        try:
            members.append(
                PoolMember(
                    account_id=item["account_id"],
                    priority=item["priority"],
                    weight=item["weight"],
                )
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("Gateway pool member is invalid.") from None
    return tuple(members)


def _parse_account_pools(value: object) -> tuple[AccountPool, ...]:
    if type(value) is not list or len(value) > 64:
        raise ValueError("Gateway account pools are invalid.")
    required = {"pool_id", "revision", "strategy", "members"}
    optional = {
        "cross_provider_fallback",
        "cross_model_fallback",
        "cross_region_fallback",
    }
    result: list[AccountPool] = []
    for item in value:
        if (
            type(item) is not dict
            or not required.issubset(item)
            or any(key not in required | optional for key in item)
        ):
            raise ValueError("Gateway account pool is invalid.")
        try:
            strategy = PoolStrategy(item["strategy"])
            result.append(
                AccountPool(
                    pool_id=item["pool_id"],
                    revision=item["revision"],
                    strategy=strategy,
                    members=_parse_pool_members(item["members"]),
                    cross_provider_fallback=item.get(
                        "cross_provider_fallback", False
                    ),
                    cross_model_fallback=item.get("cross_model_fallback", False),
                    cross_region_fallback=item.get("cross_region_fallback", False),
                )
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("Gateway account pool is invalid.") from None
    return tuple(result)


def load_gateway_config(path: Path) -> GatewayConfig:
    """Load one bounded, secret-free Gateway JSON configuration."""

    raw = _read_config(path)
    if raw is None:
        return GatewayConfig()

    return _load_gateway_config_from_raw(raw)


class GatewayConfigStore:
    def __init__(self, path: Path = DEFAULT_GATEWAY_CONFIG_PATH) -> None:
        self.path = path

    def load(self) -> GatewayConfig:
        raw = _read_private_config(self.path)
        if raw is None:
            return GatewayConfig()
        return _load_gateway_config_from_raw(raw)

    @contextmanager
    def transaction(self) -> Iterator["GatewayConfigStore"]:
        _prepare_private_parent(self.path)
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        process_lock = _process_lock_for(self.path)
        descriptor: int | None = None
        process_locked = False
        try:
            _acquire_process_lock(process_lock, deadline)
            process_locked = True
            descriptor = _open_private_lock_file(self.path)
            _acquire_file_lock(descriptor, deadline)
            yield self
        except GatewayConfigLockError:
            raise
        except Exception:
            raise
        finally:
            try:
                if descriptor is not None:
                    try:
                        _release_file_lock(descriptor)
                    finally:
                        os.close(descriptor)
            finally:
                if process_locked:
                    process_lock.release()

    def save(self, config: GatewayConfig) -> None:
        payload = _gateway_config_payload(config)
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if len(encoded) > _MAX_CONFIG_BYTES:
            raise ValueError("Gateway configuration is too large.")
        _prepare_private_parent(self.path)

        descriptor: int | None = None
        temporary: Path | None = None
        identity: tuple[int, int] | None = None
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            for _ in range(16):
                candidate = self.path.parent / (
                    f".{self.path.name}.tmp-{secrets.token_hex(16)}"
                )
                try:
                    descriptor = os.open(candidate, flags, 0o600)
                except FileExistsError:
                    continue
                temporary = candidate
                opened = os.fstat(descriptor)
                identity = (opened.st_dev, opened.st_ino)
                _harden_private_file(descriptor)
                hardened = os.fstat(descriptor)
                if not _private_regular(hardened):
                    raise ValueError("Gateway configuration file is unsafe.")
                break
            else:
                raise ValueError("Gateway configuration file could not be created.")

            assert descriptor is not None and temporary is not None
            content = memoryview(encoded)
            while content:
                written = os.write(descriptor, content)
                if written <= 0:
                    raise OSError("Gateway configuration could not be written.")
                content = content[written:]
            os.fsync(descriptor)
            completed = os.fstat(descriptor)
            if not _private_regular(completed):
                raise ValueError("Gateway configuration file is unsafe.")
            os.close(descriptor)
            descriptor = None

            os.replace(temporary, self.path)
            temporary = None
            final = self.path.lstat()
            if (
                identity is None
                or (final.st_dev, final.st_ino) != identity
                or not _private_regular(final)
            ):
                raise ValueError("Gateway configuration file is unsafe.")
            _fsync_parent(self.path)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
