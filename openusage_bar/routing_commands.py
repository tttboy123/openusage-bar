"""Bounded, credential-free route target mutations for the native app."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TextIO

from .routing_contract import RouteTarget
from .routing_targets import RouteTargetConfigError, RouteTargetStore


MAX_REQUEST_BYTES = 256 * 1024
_REQUEST_KEYS = frozenset({"version", "action", "expectedRevision", "targets"})
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


def _default_store() -> RouteTargetStore:
    return RouteTargetStore(
        Path.home() / ".local" / "state" / "openusage-bar" / "route-targets.json"
    )


def _write(
    output: TextIO,
    ok: bool,
    message: str,
    *,
    target_revision: int | None = None,
) -> int:
    payload: dict[str, object] = {"version": 1, "ok": ok, "message": message}
    if target_revision is not None:
        payload["targetRevision"] = target_revision
    output.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
    output.flush()
    return 0 if ok else 1


def run_routing_mutation(
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    store: RouteTargetStore | None = None,
) -> int:
    """Atomically replace one non-secret route target document from stdin."""
    resolved = store or _default_store()
    try:
        raw = input_stream.read(MAX_REQUEST_BYTES + 1)
        if not raw or len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("invalid request")
        payload = json.loads(raw, object_pairs_hook=_strict_object)
        request = _exact(payload, _REQUEST_KEYS)
        if request["version"] != 1 or request["action"] != "replace_targets":
            raise ValueError("invalid request")
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
    except _StaleRevision:
        return _write(
            output_stream,
            False,
            "Routing targets changed; reload before saving",
        )
    except (json.JSONDecodeError, UnicodeError, TypeError, ValueError, RouteTargetConfigError):
        return _write(output_stream, False, "Routing target request is invalid")
    except Exception:
        return _write(output_stream, False, "Routing targets could not be saved")
