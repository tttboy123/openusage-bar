"""Private Gateway account mutation command boundary."""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Protocol, TextIO

from ..keychain import default_keychain
from .accounts import AccountState, ProviderAccountRef
from .config import GatewayConfig, GatewayConfigLockError, GatewayConfigStore
from .egress import supports_provider_account_credentials
from .pools import AccountPool, PoolMember, PoolStrategy


MAX_REQUEST_BYTES = 131_072
MAX_PROVIDER_KEY_BYTES = 64 * 1024


@dataclass(frozen=True)
class _MutationRequest:
    action: str
    account: ProviderAccountRef | None = None
    secret: str | None = None
    display_id: str | None = None
    pool: dict[str, object] | None = None
    expected_revision: int | None = None


class GatewayConfigStorage(Protocol):
    def load(self) -> GatewayConfig: ...
    def save(self, config: GatewayConfig) -> None: ...


class GatewayCredentialStore(Protocol):
    def get(self, account: str) -> str | None: ...
    def set(self, account: str, secret: str) -> None: ...
    def delete(self, account: str) -> None: ...


def run_gateway_account_mutation(
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    store: GatewayConfigStorage | None = None,
    keychain: GatewayCredentialStore | None = None,
) -> int:
    try:
        raw = input_stream.read(MAX_REQUEST_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
            return _write_response(output_stream, False, "invalid_request")
        payload = json.loads(
            raw,
            object_pairs_hook=_json_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
        request = _mutation_request(payload)
        resolved_store = store or GatewayConfigStore()
        resolved_keychain = keychain if keychain is not None else default_keychain()
        transaction = getattr(resolved_store, "transaction", None)
        context = transaction() if callable(transaction) else nullcontext(resolved_store)
        public_account: ProviderAccountRef | None = None
        public_pool: dict[str, object] | None = None
        try:
            with context:
                current = resolved_store.load()
                if request.action == "create_account" and request.account is not None:
                    result = _create_account(
                        current,
                        request.account,
                        request.secret,
                        store=resolved_store,
                        keychain=resolved_keychain,
                        output_stream=output_stream,
                    )
                    if result is not None:
                        return result
                    public_account = request.account
                elif request.action == "edit_account" and request.account is not None:
                    result = _edit_account(
                        current,
                        request.account,
                        request.secret,
                        store=resolved_store,
                        keychain=resolved_keychain,
                        output_stream=output_stream,
                    )
                    if result is not None:
                        return result
                    public_account = request.account
                elif request.action == "remove_account" and request.display_id is not None:
                    result, removed = _remove_account(
                        current,
                        request.display_id,
                        store=resolved_store,
                        keychain=resolved_keychain,
                        output_stream=output_stream,
                    )
                    if result is not None:
                        return result
                    public_account = removed
                elif request.action == "create_pool" and request.pool is not None:
                    result, pool = _create_pool(
                        current,
                        request.pool,
                        store=resolved_store,
                        output_stream=output_stream,
                    )
                    if result is not None:
                        return result
                    public_pool = pool
                elif (
                    request.action == "edit_pool"
                    and request.pool is not None
                    and request.expected_revision is not None
                ):
                    result, pool = _edit_pool(
                        current,
                        request.pool,
                        request.expected_revision,
                        store=resolved_store,
                        output_stream=output_stream,
                    )
                    if result is not None:
                        return result
                    public_pool = pool
                elif (
                    request.action == "remove_pool"
                    and request.pool is not None
                    and request.expected_revision is not None
                ):
                    result, pool = _remove_pool(
                        current,
                        request.pool,
                        request.expected_revision,
                        store=resolved_store,
                        output_stream=output_stream,
                    )
                    if result is not None:
                        return result
                    public_pool = pool
                else:
                    return _write_response(output_stream, False, "invalid_request")
        except GatewayConfigLockError:
            return _write_response(output_stream, False, "lock_unavailable")
        if public_pool is not None:
            return _write_response(output_stream, True, "ok", pool=public_pool)
        if public_account is None:
            return _write_response(output_stream, False, "invalid_request")
        return _write_response(
            output_stream,
            True,
            "ok",
            account=public_account.to_public_dict(state=AccountState.UNKNOWN),
        )
    except Exception:
        return _write_response(output_stream, False, "invalid_request")


def _create_account(
    current: GatewayConfig,
    account: ProviderAccountRef,
    secret: str | None,
    *,
    store: GatewayConfigStorage,
    keychain: GatewayCredentialStore,
    output_stream: TextIO,
) -> int | None:
    if secret is None:
        return _write_response(output_stream, False, "invalid_request")
    if not supports_provider_account_credentials(account.provider_id):
        return _write_response(output_stream, False, "unsupported_provider")
    updated = GatewayConfig(
        enabled=current.enabled,
        mode=current.mode,
        host=current.host,
        port=current.port,
        proxy_enabled=current.proxy_enabled,
        cache_enabled=current.cache_enabled,
        accounts=(*current.accounts, account),
        account_pools=current.account_pools,
    )
    try:
        existing = keychain.get(account.credential_account)
    except Exception:
        return _write_response(output_stream, False, "credential_backend_unavailable")
    if existing is not None:
        return _write_response(output_stream, False, "already_exists")
    try:
        keychain.set(account.credential_account, secret)
    except Exception:
        return _write_response(output_stream, False, "credential_write_failed")
    try:
        store.save(updated)
    except Exception:
        if not _restore_credential(
            keychain,
            account.credential_account,
            old_secret=None,
        ):
            return _write_response(output_stream, False, "credential_rollback_failed")
        return _write_response(output_stream, False, "config_write_failed")
    return None


def _remove_account(
    current: GatewayConfig,
    display_id: str,
    *,
    store: GatewayConfigStorage,
    keychain: GatewayCredentialStore,
    output_stream: TextIO,
) -> tuple[int | None, ProviderAccountRef | None]:
    index = next(
        (
            position
            for position, existing in enumerate(current.accounts)
            if existing.display_id == display_id
        ),
        None,
    )
    if index is None:
        return _write_response(output_stream, False, "not_found"), None
    account = current.accounts[index]
    if any(
        member.account_id == account.account_id
        for pool in current.account_pools
        for member in pool.members
    ):
        return (
            _write_response(output_stream, False, "pool_references_account"),
            None,
        )
    try:
        old_secret = keychain.get(account.credential_account)
    except Exception:
        return (
            _write_response(output_stream, False, "credential_backend_unavailable"),
            None,
        )
    if old_secret is not None and type(old_secret) is not str:
        return (
            _write_response(output_stream, False, "credential_backend_unavailable"),
            None,
        )
    if old_secret is not None:
        try:
            keychain.delete(account.credential_account)
        except Exception:
            return _write_response(output_stream, False, "credential_delete_failed"), None

    updated_accounts = list(current.accounts)
    del updated_accounts[index]
    updated = GatewayConfig(
        enabled=current.enabled,
        mode=current.mode,
        host=current.host,
        port=current.port,
        proxy_enabled=current.proxy_enabled,
        cache_enabled=current.cache_enabled,
        accounts=tuple(updated_accounts),
        account_pools=current.account_pools,
    )
    try:
        store.save(updated)
    except Exception:
        if old_secret is not None and not _restore_credential(
            keychain,
            account.credential_account,
            old_secret=old_secret,
        ):
            return (
                _write_response(output_stream, False, "credential_rollback_failed"),
                None,
            )
        return _write_response(output_stream, False, "config_write_failed"), None
    return None, account


def _create_pool(
    current: GatewayConfig,
    pool_payload: dict[str, object],
    *,
    store: GatewayConfigStorage,
    output_stream: TextIO,
) -> tuple[int | None, dict[str, object] | None]:
    pool_id = pool_payload["poolId"]
    if any(existing.pool_id == pool_id for existing in current.account_pools):
        return _write_response(output_stream, False, "already_exists"), None

    result, members = _pool_members(current, pool_payload, output_stream)
    if result is not None:
        return result, None
    assert members is not None
    result, pool = _pool_from_payload(pool_payload, revision=1, members=members)
    if result is not None:
        return _write_response(output_stream, False, result), None

    updated = GatewayConfig(
        enabled=current.enabled,
        mode=current.mode,
        host=current.host,
        port=current.port,
        proxy_enabled=current.proxy_enabled,
        cache_enabled=current.cache_enabled,
        accounts=current.accounts,
        account_pools=(*current.account_pools, pool),
    )
    try:
        store.save(updated)
    except Exception:
        return _write_response(output_stream, False, "config_write_failed"), None
    return None, {"poolId": pool.pool_id, "revision": pool.revision}


def _edit_pool(
    current: GatewayConfig,
    pool_payload: dict[str, object],
    expected_revision: int,
    *,
    store: GatewayConfigStorage,
    output_stream: TextIO,
) -> tuple[int | None, dict[str, object] | None]:
    pool_id = pool_payload["poolId"]
    index = next(
        (
            position
            for position, existing in enumerate(current.account_pools)
            if existing.pool_id == pool_id
        ),
        None,
    )
    if index is None:
        return _write_response(output_stream, False, "not_found"), None
    existing = current.account_pools[index]
    if existing.revision != expected_revision:
        return _write_response(output_stream, False, "revision_conflict"), None
    if existing.revision >= 2**63 - 1:
        return _write_response(output_stream, False, "invalid_request"), None

    result, members = _pool_members(current, pool_payload, output_stream)
    if result is not None:
        return result, None
    assert members is not None
    result, pool = _pool_from_payload(
        pool_payload,
        revision=existing.revision + 1,
        members=members,
    )
    if result is not None:
        return _write_response(output_stream, False, result), None

    updated_pools = list(current.account_pools)
    updated_pools[index] = pool
    updated = GatewayConfig(
        enabled=current.enabled,
        mode=current.mode,
        host=current.host,
        port=current.port,
        proxy_enabled=current.proxy_enabled,
        cache_enabled=current.cache_enabled,
        accounts=current.accounts,
        account_pools=tuple(updated_pools),
    )
    try:
        store.save(updated)
    except Exception:
        return _write_response(output_stream, False, "config_write_failed"), None
    return None, {"poolId": pool.pool_id, "revision": pool.revision}


def _remove_pool(
    current: GatewayConfig,
    pool_payload: dict[str, object],
    expected_revision: int,
    *,
    store: GatewayConfigStorage,
    output_stream: TextIO,
) -> tuple[int | None, dict[str, object] | None]:
    pool_id = pool_payload["poolId"]
    index = next(
        (
            position
            for position, existing in enumerate(current.account_pools)
            if existing.pool_id == pool_id
        ),
        None,
    )
    if index is None:
        return _write_response(output_stream, False, "not_found"), None
    existing = current.account_pools[index]
    if existing.revision != expected_revision:
        return _write_response(output_stream, False, "revision_conflict"), None

    updated_pools = list(current.account_pools)
    del updated_pools[index]
    updated = GatewayConfig(
        enabled=current.enabled,
        mode=current.mode,
        host=current.host,
        port=current.port,
        proxy_enabled=current.proxy_enabled,
        cache_enabled=current.cache_enabled,
        accounts=current.accounts,
        account_pools=tuple(updated_pools),
    )
    try:
        store.save(updated)
    except Exception:
        return _write_response(output_stream, False, "config_write_failed"), None
    return None, {"poolId": existing.pool_id, "revision": existing.revision}


def _pool_members(
    current: GatewayConfig,
    pool_payload: dict[str, object],
    output_stream: TextIO,
) -> tuple[int | None, list[PoolMember] | None]:
    account_ids_by_display_id = {
        account.display_id: account.account_id for account in current.accounts
    }
    members: list[PoolMember] = []
    for member_payload in pool_payload["members"]:
        assert type(member_payload) is dict
        display_id = member_payload["displayId"]
        if display_id not in account_ids_by_display_id:
            return (
                _write_response(
                    output_stream,
                    False,
                    "pool_references_unknown_account",
                ),
                None,
            )
        try:
            members.append(
                PoolMember(
                    account_id=account_ids_by_display_id[display_id],
                    priority=member_payload["priority"],
                    weight=member_payload["weight"],
                )
            )
        except Exception:
            return _write_response(output_stream, False, "invalid_request"), None
    return None, members


def _pool_from_payload(
    pool_payload: dict[str, object],
    *,
    revision: int,
    members: list[PoolMember],
) -> tuple[str | None, AccountPool | None]:
    try:
        pool = AccountPool(
            pool_id=pool_payload["poolId"],
            revision=revision,
            strategy=PoolStrategy(pool_payload["strategy"]),
            members=tuple(members),
            cross_provider_fallback=pool_payload["crossProviderFallback"],
            cross_model_fallback=pool_payload["crossModelFallback"],
            cross_region_fallback=pool_payload["crossRegionFallback"],
        )
    except Exception:
        return "invalid_request", None
    return None, pool


def _edit_account(
    current: GatewayConfig,
    account: ProviderAccountRef,
    secret: str | None,
    *,
    store: GatewayConfigStorage,
    keychain: GatewayCredentialStore,
    output_stream: TextIO,
) -> int | None:
    if not supports_provider_account_credentials(account.provider_id):
        return _write_response(output_stream, False, "unsupported_provider")
    index = next(
        (
            position
            for position, existing in enumerate(current.accounts)
            if existing.provider_id == account.provider_id
            and existing.account_id == account.account_id
        ),
        None,
    )
    if index is None:
        return _write_response(output_stream, False, "not_found")

    updated_accounts = list(current.accounts)
    updated_accounts[index] = account
    updated = GatewayConfig(
        enabled=current.enabled,
        mode=current.mode,
        host=current.host,
        port=current.port,
        proxy_enabled=current.proxy_enabled,
        cache_enabled=current.cache_enabled,
        accounts=tuple(updated_accounts),
        account_pools=current.account_pools,
    )
    if secret is None:
        try:
            store.save(updated)
        except Exception:
            return _write_response(output_stream, False, "config_write_failed")
        return None

    try:
        old_secret = keychain.get(account.credential_account)
    except Exception:
        return _write_response(output_stream, False, "credential_backend_unavailable")
    if old_secret is not None and type(old_secret) is not str:
        return _write_response(output_stream, False, "credential_backend_unavailable")

    try:
        keychain.set(account.credential_account, secret)
    except Exception:
        return _write_response(output_stream, False, "credential_write_failed")
    try:
        store.save(updated)
    except Exception:
        if not _restore_credential(
            keychain,
            account.credential_account,
            old_secret=old_secret,
        ):
            return _write_response(output_stream, False, "credential_rollback_failed")
        return _write_response(output_stream, False, "config_write_failed")
    return None


def _restore_credential(
    keychain: GatewayCredentialStore,
    credential_account: str,
    *,
    old_secret: str | None,
) -> bool:
    try:
        if old_secret is None:
            keychain.delete(credential_account)
        else:
            keychain.set(credential_account, old_secret)
    except Exception:
        return False
    return True


def _json_object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid request")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError("invalid request")


def _mutation_request(value: object) -> _MutationRequest:
    if type(value) is not dict:
        raise ValueError("invalid request")
    action = value.get("action")
    if value.get("version") != 1 or action not in {
        "create_account",
        "edit_account",
        "remove_account",
        "create_pool",
        "edit_pool",
        "remove_pool",
    }:
        raise ValueError("invalid request")
    if action in {"create_pool", "edit_pool", "remove_pool"}:
        return _pool_mutation_request(value, action)

    if set(value) != {"version", "action", "account", "credentialMaterial"}:
        raise ValueError("invalid request")
    account_payload = value.get("account")
    credential_payload = value.get("credentialMaterial")
    if action == "remove_account":
        if type(account_payload) is not dict or set(account_payload) != {
            "displayId",
        }:
            raise ValueError("invalid request")
        if type(credential_payload) is not dict or set(credential_payload) != set():
            raise ValueError("invalid request")
        display_id = account_payload.get("displayId")
        if not _valid_display_id(display_id):
            raise ValueError("invalid request")
        assert type(action) is str and type(display_id) is str
        return _MutationRequest(action=action, display_id=display_id)

    if type(account_payload) is not dict or set(account_payload) != {
        "providerId",
        "accountId",
        "alias",
    }:
        raise ValueError("invalid request")
    if type(credential_payload) is not dict or set(credential_payload) != {
        "providerKey",
    }:
        raise ValueError("invalid request")
    provider_id = account_payload.get("providerId")
    account_id = account_payload.get("accountId")
    alias = account_payload.get("alias")
    secret = credential_payload.get("providerKey")
    if type(provider_id) is not str or type(account_id) is not str:
        raise ValueError("invalid request")
    if action == "create_account" and not _valid_provider_key(secret):
        raise ValueError("invalid request")
    if action == "edit_account" and secret is not None and not _valid_provider_key(secret):
        raise ValueError("invalid request")
    account = ProviderAccountRef(
        provider_id=provider_id,
        account_id=account_id,
        alias=alias,
        credential_account=f"{provider_id}.{account_id}.gateway-api-key",
    )
    assert type(action) is str
    return _MutationRequest(action=action, account=account, secret=secret)


def _pool_mutation_request(
    value: dict[Any, Any],
    action: object,
) -> _MutationRequest:
    if set(value) != {"version", "action", "pool", "expectedRevision"}:
        raise ValueError("invalid request")
    expected_revision = value.get("expectedRevision")
    if action == "create_pool" and expected_revision is not None:
        raise ValueError("invalid request")
    if (
        action == "edit_pool"
        and (type(expected_revision) is not int or expected_revision < 1)
    ):
        raise ValueError("invalid request")
    if (
        action == "remove_pool"
        and (type(expected_revision) is not int or expected_revision < 1)
    ):
        raise ValueError("invalid request")
    pool_payload = value.get("pool")
    if action == "remove_pool":
        if type(pool_payload) is not dict or set(pool_payload) != {"poolId"}:
            raise ValueError("invalid request")
        pool_id = pool_payload.get("poolId")
        if type(pool_id) is not str:
            raise ValueError("invalid request")
        assert type(action) is str and type(expected_revision) is int
        return _MutationRequest(
            action=action,
            expected_revision=expected_revision,
            pool={"poolId": pool_id},
        )
    if type(pool_payload) is not dict or set(pool_payload) != {
        "poolId",
        "strategy",
        "members",
        "crossProviderFallback",
        "crossModelFallback",
        "crossRegionFallback",
    }:
        raise ValueError("invalid request")
    pool_id = pool_payload.get("poolId")
    strategy = pool_payload.get("strategy")
    members = pool_payload.get("members")
    if type(pool_id) is not str or type(strategy) is not str or type(members) is not list:
        raise ValueError("invalid request")
    for key in (
        "crossProviderFallback",
        "crossModelFallback",
        "crossRegionFallback",
    ):
        if type(pool_payload.get(key)) is not bool:
            raise ValueError("invalid request")
    parsed_members: list[dict[str, object]] = []
    for member in members:
        if type(member) is not dict or set(member) != {
            "displayId",
            "priority",
            "weight",
        }:
            raise ValueError("invalid request")
        display_id = member.get("displayId")
        priority = member.get("priority")
        weight = member.get("weight")
        if (
            not _valid_display_id(display_id)
            or type(priority) is not int
            or type(weight) is not int
        ):
            raise ValueError("invalid request")
        parsed_members.append(
            {
                "displayId": display_id,
                "priority": priority,
                "weight": weight,
            }
        )
    assert type(action) is str
    return _MutationRequest(
        action=action,
        expected_revision=expected_revision,
        pool={
            "poolId": pool_id,
            "strategy": strategy,
            "members": parsed_members,
            "crossProviderFallback": pool_payload["crossProviderFallback"],
            "crossModelFallback": pool_payload["crossModelFallback"],
            "crossRegionFallback": pool_payload["crossRegionFallback"],
        },
    )


def _valid_provider_key(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value.isascii()
        and len(value.encode("ascii")) <= MAX_PROVIDER_KEY_BYTES
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _valid_display_id(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 17
        and value.startswith("acct_")
        and all(character in "0123456789abcdef" for character in value[5:])
    )


def _write_response(
    output_stream: TextIO,
    ok: bool,
    code: str,
    *,
    account: dict[str, object] | None = None,
    pool: dict[str, object] | None = None,
) -> int:
    payload: dict[str, object] = {"version": 1, "ok": ok, "code": code}
    if account is not None:
        payload["account"] = account
    if pool is not None:
        payload["pool"] = pool
    json.dump(payload, output_stream, ensure_ascii=True, separators=(",", ":"))
    output_stream.write("\n")
    output_stream.flush()
    return 0 if ok else 1


__all__ = ["run_gateway_account_mutation"]
