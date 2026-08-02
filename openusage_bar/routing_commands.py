"""Bounded, credential-free route target mutations for the native app."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TextIO

from .keychain import MacOSKeychain
from .routing_contract import RouteTarget
from .routing_execution import (
    ExecutionConnection,
    ExecutionConnectionConfigError,
    ExecutionConnectionStore,
    credential_account,
)
from .routing_targets import RouteTargetConfigError, RouteTargetStore


MAX_REQUEST_BYTES = 256 * 1024
_REQUEST_KEYS = frozenset({"version", "action", "expectedRevision", "targets"})
_CONNECTION_REQUEST_KEYS = frozenset({
    "version", "action", "expectedRevision", "connection", "secret",
})
_REMOVE_CONNECTION_REQUEST_KEYS = frozenset({
    "version", "action", "expectedRevision", "connectionRef",
})
_LIST_CONNECTION_REQUEST_KEYS = frozenset({"version", "action"})
_CONNECTION_KEYS = frozenset({
    "connectionRef", "providerId", "accountRef", "executionClass",
    "executionAdapterId", "baseURL", "enabled", "models",
})
_TARGET_KEYS = frozenset({
    "targetId",
    "providerId",
    "accountRef",
    "modelId",
    "connectionRef",
    "executionClass",
    "executionAdapterId",
    "resourceMode",
    "factAccountRef",
    "runtimeScopeRef",
    "balanceCurrency",
    "costCurrency",
    "inputCostMicrosPerMillion",
    "outputCostMicrosPerMillion",
    "enabled",
    "regions",
    "privacyClass",
    "capabilities",
    "contextWindowTokens",
    "qualityTier",
})


class _StaleRevision(ValueError):
    pass


class _StaleConnectionRevision(ValueError):
    pass


class _ConnectionConflict(ValueError):
    pass


class _ConnectionInUse(ValueError):
    pass


class _ConnectionSaveFailure(RuntimeError):
    pass


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _exact(value: object, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise ValueError("invalid object")
    return value


def _ids(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > 50
        or any(not isinstance(item, str) for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("invalid identifier list")
    return tuple(value)


def _models(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 256
        or any(not isinstance(item, str) for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("invalid model list")
    return tuple(value)


def _optional_string(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValueError("invalid optional string")


def _decode_target(value: object) -> RouteTarget:
    raw = _exact(value, _TARGET_KEYS)
    if not isinstance(raw["enabled"], bool):
        raise ValueError("invalid enabled state")
    try:
        return RouteTarget(
            target_id=raw["targetId"],
            provider_id=raw["providerId"],
            account_ref=raw["accountRef"],
            model_id=raw["modelId"],
            connection_ref=raw["connectionRef"],
            execution_class=raw["executionClass"],
            execution_adapter_id=raw["executionAdapterId"],
            resource_mode=raw["resourceMode"],
            fact_account_ref=_optional_string(raw["factAccountRef"]),
            runtime_scope_ref=_optional_string(raw["runtimeScopeRef"]),
            balance_currency=_optional_string(raw["balanceCurrency"]),
            cost_currency=_optional_string(raw["costCurrency"]),
            input_cost_micros_per_million=raw["inputCostMicrosPerMillion"],
            output_cost_micros_per_million=raw["outputCostMicrosPerMillion"],
            enabled=raw["enabled"],
            adapter_available=False,
            regions=_ids(raw["regions"]),
            privacy_class=raw["privacyClass"],
            capabilities=_ids(raw["capabilities"]),
            context_window_tokens=raw["contextWindowTokens"],
            quality_tier=raw["qualityTier"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid target") from error


def _decode_connection(value: object) -> ExecutionConnection:
    raw = _exact(value, _CONNECTION_KEYS)
    if not isinstance(raw["enabled"], bool):
        raise ValueError("invalid enabled state")
    try:
        return ExecutionConnection(
            connection_ref=raw["connectionRef"],
            provider_id=raw["providerId"],
            account_ref=raw["accountRef"],
            execution_class=raw["executionClass"],
            execution_adapter_id=raw["executionAdapterId"],
            base_url=raw["baseURL"],
            enabled=raw["enabled"],
            models=_models(raw["models"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid execution connection") from error


def _default_store() -> RouteTargetStore:
    return RouteTargetStore(
        Path.home() / ".local" / "state" / "openusage-bar" / "route-targets.json"
    )


def _default_connection_store() -> ExecutionConnectionStore:
    return ExecutionConnectionStore(
        Path.home()
        / ".local" / "state" / "openusage-bar"
        / "execution-connections.json"
    )


def _compatible(target: RouteTarget, connection: ExecutionConnection) -> bool:
    return (
        target.provider_id == connection.provider_id
        and target.account_ref == connection.account_ref
        and target.execution_class == connection.execution_class
        and target.execution_adapter_id == connection.execution_adapter_id
        and target.model_id in connection.models
    )


def _connection_wire(value: ExecutionConnection) -> dict[str, object]:
    return {
        "connectionRef": value.connection_ref,
        "providerId": value.provider_id,
        "accountRef": value.account_ref,
        "executionClass": value.execution_class,
        "executionAdapterId": value.execution_adapter_id,
        "baseURL": value.base_url,
        "enabled": value.enabled,
        "models": list(value.models),
    }


def _restore_secret(
    keychain: MacOSKeychain,
    account: str,
    previous: str | None,
) -> None:
    try:
        if previous is None:
            keychain.delete(account)
        else:
            keychain.set(account, previous)
    except Exception:
        pass


def _write(
    output: TextIO,
    ok: bool,
    message: str,
    *,
    target_revision: int | None = None,
    connection_revision: int | None = None,
    connections: tuple[ExecutionConnection, ...] | None = None,
) -> int:
    payload: dict[str, object] = {"version": 1, "ok": ok, "message": message}
    if target_revision is not None:
        payload["targetRevision"] = target_revision
    if connection_revision is not None:
        payload["connectionRevision"] = connection_revision
    if connections is not None:
        payload["connections"] = [_connection_wire(value) for value in connections]
    output.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
    output.flush()
    return 0 if ok else 1


def run_routing_mutation(
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    store: RouteTargetStore | None = None,
    connection_store: ExecutionConnectionStore | None = None,
    keychain: MacOSKeychain | None = None,
) -> int:
    """Mutate bounded route configuration from one private stdin document."""
    resolved = store or _default_store()
    resolved_connections = connection_store or _default_connection_store()
    resolved_keychain = keychain

    def mutation_keychain() -> MacOSKeychain:
        nonlocal resolved_keychain
        if resolved_keychain is None:
            resolved_keychain = MacOSKeychain()
        return resolved_keychain
    try:
        raw = input_stream.read(MAX_REQUEST_BYTES + 1)
        if not raw or len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("invalid request")
        payload = json.loads(raw, object_pairs_hook=_strict_object)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("invalid request")
        action = payload.get("action")
        if action == "list_connections":
            _exact(payload, _LIST_CONNECTION_REQUEST_KEYS)
            configuration = resolved_connections.load()
            return _write(
                output_stream, True, "Execution connections loaded",
                connection_revision=configuration.revision,
                connections=configuration.connections,
            )
        if action == "replace_targets":
            request = _exact(payload, _REQUEST_KEYS)
            expected = request["expectedRevision"]
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                raise ValueError("invalid revision")
            values = request["targets"]
            if not isinstance(values, list) or len(values) > 128:
                raise ValueError("invalid targets")
            targets = tuple(_decode_target(value) for value in values)
            if len({value.target_id for value in targets}) != len(targets):
                raise ValueError("duplicate target")
            current = resolved.load(available_adapters=()).revision
            if current != expected:
                raise _StaleRevision()
            revision = expected + 1
            resolved.save(targets, revision=revision)
            return _write(
                output_stream,
                True,
                "Routing targets saved",
                target_revision=revision,
            )
        if action == "upsert_connection":
            request = _exact(payload, _CONNECTION_REQUEST_KEYS)
            expected = request["expectedRevision"]
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                raise ValueError("invalid revision")
            secret = request["secret"]
            if (
                not isinstance(secret, str)
                or len(secret.encode("utf-8")) > 65_536
                or "\r" in secret
                or "\n" in secret
            ):
                raise ValueError("invalid secret")
            connection = _decode_connection(request["connection"])
            configuration = resolved_connections.load()
            if configuration.revision != expected:
                raise _StaleConnectionRevision()
            existing = next((
                value for value in configuration.connections
                if value.connection_ref == connection.connection_ref
            ), None)
            if existing is None and not secret:
                raise ValueError("new connection requires secret")
            targets = resolved.load(available_adapters=()).targets
            if any(
                value.connection_ref == connection.connection_ref
                and not _compatible(value, connection)
                for value in targets
            ):
                raise _ConnectionConflict()
            account = credential_account(connection.connection_ref)
            previous: str | None = None
            if secret:
                try:
                    active_keychain = mutation_keychain()
                    previous = active_keychain.get(account)
                    active_keychain.set(account, secret)
                except Exception as error:
                    raise _ConnectionSaveFailure() from error
            updated = tuple(
                connection if value.connection_ref == connection.connection_ref else value
                for value in configuration.connections
            )
            if existing is None:
                updated = (*updated, connection)
            revision = expected + 1
            try:
                resolved_connections.save(updated, revision=revision)
            except Exception as error:
                if secret:
                    _restore_secret(mutation_keychain(), account, previous)
                raise _ConnectionSaveFailure() from error
            return _write(
                output_stream, True, "Execution connection saved",
                connection_revision=revision,
            )
        if action == "remove_connection":
            request = _exact(payload, _REMOVE_CONNECTION_REQUEST_KEYS)
            expected = request["expectedRevision"]
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                raise ValueError("invalid revision")
            connection_ref = request["connectionRef"]
            if not isinstance(connection_ref, str):
                raise ValueError("invalid connection")
            account = credential_account(connection_ref)
            configuration = resolved_connections.load()
            if configuration.revision != expected:
                raise _StaleConnectionRevision()
            existing = next((
                value for value in configuration.connections
                if value.connection_ref == connection_ref
            ), None)
            if existing is None:
                raise ValueError("connection missing")
            if any(
                value.connection_ref == connection_ref
                for value in resolved.load(available_adapters=()).targets
            ):
                raise _ConnectionInUse()
            try:
                active_keychain = mutation_keychain()
                previous = active_keychain.get(account)
                active_keychain.delete(account)
            except Exception as error:
                raise _ConnectionSaveFailure() from error
            revision = expected + 1
            try:
                resolved_connections.save(tuple(
                    value for value in configuration.connections
                    if value.connection_ref != connection_ref
                ), revision=revision)
            except Exception as error:
                _restore_secret(mutation_keychain(), account, previous)
                raise _ConnectionSaveFailure() from error
            return _write(
                output_stream, True, "Execution connection removed",
                connection_revision=revision,
            )
        raise ValueError("invalid request")
    except _StaleRevision:
        return _write(
            output_stream,
            False,
            "Routing targets changed; reload before saving",
        )
    except _StaleConnectionRevision:
        return _write(
            output_stream, False,
            "Execution connections changed; reload before saving",
        )
    except _ConnectionConflict:
        return _write(
            output_stream, False,
            "Execution connection conflicts with routing targets",
        )
    except _ConnectionInUse:
        return _write(
            output_stream, False,
            "Execution connection is used by routing targets",
        )
    except _ConnectionSaveFailure:
        return _write(
            output_stream, False,
            "Execution connection could not be saved",
        )
    except (
        json.JSONDecodeError, UnicodeError, TypeError, ValueError,
        RouteTargetConfigError, ExecutionConnectionConfigError,
    ):
        return _write(output_stream, False, "Routing target request is invalid")
    except Exception:
        return _write(output_stream, False, "Routing targets could not be saved")
