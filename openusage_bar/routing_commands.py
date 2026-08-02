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
from .routing_policy import RoutePolicy, built_in_policy_ids
from .routing_policy_store import (
    RoutingPolicyConfigError,
    RoutingPolicyStore,
)
from .routing_preferences import (
    RoutingPreferences,
    RoutingPreferencesConfigError,
    RoutingPreferencesStore,
)


MAX_REQUEST_BYTES = 256 * 1024
_REQUEST_KEYS = frozenset({"version", "action", "expectedRevision", "targets"})
_CONNECTION_REQUEST_KEYS = frozenset({
    "version", "action", "expectedRevision", "connection", "secret",
})
_REMOVE_CONNECTION_REQUEST_KEYS = frozenset({
    "version", "action", "expectedRevision", "connectionRef",
})
_LIST_CONNECTION_REQUEST_KEYS = frozenset({"version", "action"})
_POLICY_REQUEST_KEYS = frozenset({
    "version", "action", "expectedRevision", "policy",
})
_REMOVE_POLICY_REQUEST_KEYS = frozenset({
    "version", "action", "expectedRevision", "policyId",
})
_LIST_POLICY_REQUEST_KEYS = frozenset({"version", "action"})
_GET_PREFERENCES_REQUEST_KEYS = frozenset({"version", "action"})
_SET_PREFERENCES_REQUEST_KEYS = frozenset({
    "version", "action", "expectedRevision", "decisionApiEnabled",
    "defaultPolicyId",
})
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
_POLICY_KEYS = frozenset({
    "policyId", "reliabilityWeight", "headroomWeight", "latencyWeight",
    "costWeight", "minimumHeadroomBasisPoints", "maximumErrorRateBasisPoints",
    "minimumRuntimeSamples", "latencyReferenceMilliseconds",
    "costReferenceMicrounits", "costCurrency", "minimumBalanceMicrounits",
    "balanceReferenceMicrounits", "unknownPenaltyBasisPoints", "requireCost",
    "requireRuntime", "allowedExecutionClasses", "allowedPrivacyClasses",
})


class _StaleRevision(ValueError):
    pass


class _StaleConnectionRevision(ValueError):
    pass


class _StalePolicyRevision(ValueError):
    pass


class _StalePreferencesRevision(ValueError):
    pass


class _DefaultPolicyInUse(ValueError):
    pass


class _ConnectionConflict(ValueError):
    pass


class _ConnectionInUse(ValueError):
    pass


class _TargetConnectionMismatch(ValueError):
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


def _decode_policy(value: object, *, revision: int) -> RoutePolicy:
    raw = _exact(value, _POLICY_KEYS)
    try:
        return RoutePolicy(
            policy_id=raw["policyId"],
            revision=revision,
            reliability_weight=raw["reliabilityWeight"],
            headroom_weight=raw["headroomWeight"],
            latency_weight=raw["latencyWeight"],
            cost_weight=raw["costWeight"],
            min_headroom_bp=raw["minimumHeadroomBasisPoints"],
            max_error_rate_bp=raw["maximumErrorRateBasisPoints"],
            min_runtime_samples=raw["minimumRuntimeSamples"],
            latency_reference_ms=raw["latencyReferenceMilliseconds"],
            cost_reference_micros=raw["costReferenceMicrounits"],
            cost_currency=raw["costCurrency"],
            min_balance_micros=raw["minimumBalanceMicrounits"],
            balance_reference_micros=raw["balanceReferenceMicrounits"],
            unknown_penalty=raw["unknownPenaltyBasisPoints"],
            require_cost=raw["requireCost"],
            require_runtime=raw["requireRuntime"],
            allowed_execution_classes=frozenset(_ids(raw["allowedExecutionClasses"])),
            allowed_privacy_classes=frozenset(_ids(raw["allowedPrivacyClasses"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid custom routing policy") from error


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


def _default_policy_store() -> RoutingPolicyStore:
    return RoutingPolicyStore(
        Path.home() / ".local" / "state" / "openusage-bar" / "routing-policies.json"
    )


def _default_preferences_store() -> RoutingPreferencesStore:
    return RoutingPreferencesStore(
        Path.home() / ".local" / "state" / "openusage-bar"
        / "routing-preferences.json"
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


def _policy_wire(value: RoutePolicy) -> dict[str, object]:
    return {
        "policyId": value.policy_id,
        "policyRevision": value.revision,
        "reliabilityWeight": value.reliability_weight,
        "headroomWeight": value.headroom_weight,
        "latencyWeight": value.latency_weight,
        "costWeight": value.cost_weight,
        "minimumHeadroomBasisPoints": value.min_headroom_bp,
        "maximumErrorRateBasisPoints": value.max_error_rate_bp,
        "minimumRuntimeSamples": value.min_runtime_samples,
        "latencyReferenceMilliseconds": value.latency_reference_ms,
        "costReferenceMicrounits": value.cost_reference_micros,
        "costCurrency": value.cost_currency,
        "minimumBalanceMicrounits": value.min_balance_micros,
        "balanceReferenceMicrounits": value.balance_reference_micros,
        "unknownPenaltyBasisPoints": value.unknown_penalty,
        "requireCost": value.require_cost,
        "requireRuntime": value.require_runtime,
        "allowedExecutionClasses": sorted(value.allowed_execution_classes),
        "allowedPrivacyClasses": sorted(value.allowed_privacy_classes),
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
    policy_document_revision: int | None = None,
    custom_policies: tuple[RoutePolicy, ...] | None = None,
    preferences_revision: int | None = None,
    routing_preferences: RoutingPreferences | None = None,
) -> int:
    payload: dict[str, object] = {"version": 1, "ok": ok, "message": message}
    if target_revision is not None:
        payload["targetRevision"] = target_revision
    if connection_revision is not None:
        payload["connectionRevision"] = connection_revision
    if connections is not None:
        payload["connections"] = [_connection_wire(value) for value in connections]
    if policy_document_revision is not None:
        payload["policyDocumentRevision"] = policy_document_revision
    if custom_policies is not None:
        payload["customPolicies"] = [_policy_wire(value) for value in custom_policies]
    if preferences_revision is not None:
        payload["preferencesRevision"] = preferences_revision
    if routing_preferences is not None:
        payload["routingPreferences"] = {
            "decisionApiEnabled": routing_preferences.decision_api_enabled,
            "defaultPolicyId": routing_preferences.default_policy_id,
        }
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
    policy_store: RoutingPolicyStore | None = None,
    preferences_store: RoutingPreferencesStore | None = None,
) -> int:
    """Mutate bounded route configuration from one private stdin document."""
    resolved = store or _default_store()
    resolved_connections = connection_store or _default_connection_store()
    resolved_policies = policy_store or _default_policy_store()
    resolved_preferences = preferences_store or _default_preferences_store()
    resolved_keychain = keychain

    def mutation_keychain() -> MacOSKeychain:
        nonlocal resolved_keychain
        if resolved_keychain is None:
            resolved_keychain = MacOSKeychain()
        return resolved_keychain
    payload: object = None
    try:
        raw = input_stream.read(MAX_REQUEST_BYTES + 1)
        if not raw or len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("invalid request")
        payload = json.loads(raw, object_pairs_hook=_strict_object)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("invalid request")
        action = payload.get("action")
        if action == "get_preferences":
            _exact(payload, _GET_PREFERENCES_REQUEST_KEYS)
            preferences = resolved_preferences.load()
            return _write(
                output_stream, True, "Routing preferences loaded",
                preferences_revision=preferences.revision,
                routing_preferences=preferences,
            )
        if action == "set_preferences":
            request = _exact(payload, _SET_PREFERENCES_REQUEST_KEYS)
            expected = request["expectedRevision"]
            enabled = request["decisionApiEnabled"]
            policy_id = request["defaultPolicyId"]
            if (
                isinstance(expected, bool) or not isinstance(expected, int)
                or expected < 0 or not isinstance(enabled, bool)
                or not isinstance(policy_id, str)
            ):
                raise ValueError("invalid routing preferences")
            current = resolved_preferences.load()
            if current.revision != expected:
                raise _StalePreferencesRevision()
            available_policy_ids = built_in_policy_ids() | {
                value.policy_id for value in resolved_policies.load().policies
            }
            if policy_id not in available_policy_ids:
                raise ValueError("invalid routing preferences")
            preferences = RoutingPreferences(expected + 1, enabled, policy_id)
            resolved_preferences.save(preferences)
            return _write(
                output_stream, True, "Routing preferences saved",
                preferences_revision=preferences.revision,
            )
        if action == "list_policies":
            _exact(payload, _LIST_POLICY_REQUEST_KEYS)
            configuration = resolved_policies.load()
            return _write(
                output_stream, True, "Custom routing policies loaded",
                policy_document_revision=configuration.revision,
                custom_policies=configuration.policies,
            )
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
            connections = {
                value.connection_ref: value
                for value in resolved_connections.load().connections
            }
            if any(
                target.connection_ref not in connections
                or not _compatible(target, connections[target.connection_ref])
                for target in targets
            ):
                raise _TargetConnectionMismatch()
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
        if action == "upsert_policy":
            request = _exact(payload, _POLICY_REQUEST_KEYS)
            expected = request["expectedRevision"]
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                raise ValueError("invalid revision")
            raw_policy = _exact(request["policy"], _POLICY_KEYS)
            policy_id = raw_policy["policyId"]
            if not isinstance(policy_id, str) or policy_id in built_in_policy_ids():
                raise ValueError("invalid custom policy")
            configuration = resolved_policies.load()
            if configuration.revision != expected:
                raise _StalePolicyRevision()
            existing = next((
                value for value in configuration.policies
                if value.policy_id == policy_id
            ), None)
            policy = _decode_policy(
                raw_policy,
                revision=1 if existing is None else existing.revision + 1,
            )
            updated = tuple(
                policy if value.policy_id == policy_id else value
                for value in configuration.policies
            )
            if existing is None:
                updated = (*updated, policy)
            revision = expected + 1
            resolved_policies.save(updated, revision=revision)
            return _write(
                output_stream, True, "Custom routing policy saved",
                policy_document_revision=revision,
            )
        if action == "remove_policy":
            request = _exact(payload, _REMOVE_POLICY_REQUEST_KEYS)
            expected = request["expectedRevision"]
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                raise ValueError("invalid revision")
            policy_id = request["policyId"]
            if not isinstance(policy_id, str) or policy_id in built_in_policy_ids():
                raise ValueError("invalid custom policy")
            configuration = resolved_policies.load()
            if configuration.revision != expected:
                raise _StalePolicyRevision()
            if resolved_preferences.load().default_policy_id == policy_id:
                raise _DefaultPolicyInUse()
            if not any(value.policy_id == policy_id for value in configuration.policies):
                raise ValueError("custom policy missing")
            revision = expected + 1
            resolved_policies.save(tuple(
                value for value in configuration.policies
                if value.policy_id != policy_id
            ), revision=revision)
            return _write(
                output_stream, True, "Custom routing policy removed",
                policy_document_revision=revision,
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
    except _StalePolicyRevision:
        return _write(
            output_stream, False,
            "Routing policies changed; reload before saving",
        )
    except _StalePreferencesRevision:
        return _write(
            output_stream, False,
            "Routing preferences changed; reload before saving",
        )
    except _DefaultPolicyInUse:
        return _write(
            output_stream, False,
            "Default routing policy cannot be removed",
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
    except _TargetConnectionMismatch:
        return _write(
            output_stream, False,
            "Routing target does not match an execution connection",
        )
    except _ConnectionSaveFailure:
        return _write(
            output_stream, False,
            "Execution connection could not be saved",
        )
    except (
        json.JSONDecodeError, UnicodeError, TypeError, ValueError,
        RouteTargetConfigError, ExecutionConnectionConfigError,
        RoutingPolicyConfigError, RoutingPreferencesConfigError,
    ):
        message = (
            "Routing preferences request is invalid"
            if isinstance(payload, dict)
            and payload.get("action") in {"get_preferences", "set_preferences"}
            else (
                "Custom routing policy request is invalid"
                if isinstance(payload, dict)
                and payload.get("action") in {"upsert_policy", "remove_policy"}
                else "Routing target request is invalid"
            )
        )
        return _write(output_stream, False, message)
    except Exception:
        return _write(output_stream, False, "Routing targets could not be saved")
