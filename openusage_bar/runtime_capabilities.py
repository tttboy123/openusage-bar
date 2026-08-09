"""Build and validate the closed renderer-safe runtime capability snapshot."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime


API_VERSION = "runtime-capability.openusage/v1"
OBJECT_TYPE = "runtime.capability"
SERIALIZED_UTF8_LIMIT = 64 * 1024
MAX_TEXT_LENGTH = 256
MAX_SCHEMA_VERSION_LENGTH = 64

SUPPORT_VALUES = ("supported", "unsupported", "unknown")
OPERATIONAL_VALUES = (
    "disabled",
    "starting",
    "ready",
    "degraded",
    "unavailable",
    "unknown",
)
GATEWAY_MODES = ("observe", "advise", "gateway", "unknown")
FEATURE_IDS = (
    "listener",
    "should_send",
    "responses",
    "cache",
    "fallback",
    "pii_redaction",
    "streaming",
)
ACTION_IDS = (
    "enable",
    "disable",
    "open_settings",
    "retry",
    "learn_more",
    "clear_cache",
)
SAFE_ERROR_CODES = (
    "gateway_disabled",
    "gateway_starting",
    "gateway_timeout",
    "gateway_unavailable",
    "provider_unavailable",
    "credential_backend_unavailable",
    "capability_invalid",
)

_MAX_INPUT_DEPTH = 16
_MAX_INPUT_NODES = 4096
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$"
)
_SCHEMA_VERSION = re.compile(r"^(?:openusage/v\d+|\d+\.\d+)$")
_INVALID = object()


def build_runtime_capability(
    observer: object,
    gateway: object,
    *,
    platform: str | None = None,
) -> dict[str, object]:
    """Return platform-neutral safe facts without inferring runtime health."""
    del platform
    return {
        "apiVersion": API_VERSION,
        "object": OBJECT_TYPE,
        "observer": _normalize_observer(observer),
        "gateway": _normalize_gateway(gateway),
    }


def validate_runtime_capability(value: object) -> dict[str, object] | None:
    """Normalize an untrusted envelope, rejecting a foreign top-level type."""
    if type(value) is not dict or any(type(key) is not str for key in value):
        return None
    if (
        type(value.get("apiVersion")) is not str
        or value.get("apiVersion") != API_VERSION
        or type(value.get("object")) is not str
        or value.get("object") != OBJECT_TYPE
        or "observer" not in value
        or "gateway" not in value
    ):
        return None

    additive = {
        key: item
        for key, item in value.items()
        if key not in {"apiVersion", "object", "observer", "gateway"}
    }
    if not _is_bounded_json(additive):
        return None
    return build_runtime_capability(value["observer"], value["gateway"])


def _normalize_observer(value: object) -> dict[str, object]:
    if type(value) is not dict or not _is_bounded_json(value):
        return _unknown_observer()

    operational = value.get("operational")
    if operational not in OPERATIONAL_VALUES:
        operational = "unknown"
    generated_at = _nullable_timestamp(value.get("generatedAt"))
    last_good_at = _nullable_timestamp(value.get("lastGoodAt"))
    data_revision = _nullable_count(value.get("dataRevision"))
    schema_version = value.get("schemaVersion")
    if not (
        type(schema_version) is str
        and len(schema_version) <= MAX_SCHEMA_VERSION_LENGTH
        and _SCHEMA_VERSION.fullmatch(schema_version)
    ):
        schema_version = None
    return {
        "operational": operational,
        "generatedAt": generated_at,
        "lastGoodAt": last_good_at,
        "dataRevision": None if data_revision is _INVALID else data_revision,
        "schemaVersion": schema_version,
    }


def _normalize_gateway(value: object) -> dict[str, object]:
    normalized = _validated_gateway(value)
    return _unknown_gateway() if normalized is None else normalized


def _validated_gateway(value: object) -> dict[str, object] | None:
    if type(value) is not dict or not _is_bounded_json(value):
        return None

    raw_mode = value.get("mode", _INVALID)
    if type(raw_mode) is not str:
        return None
    mode = raw_mode if raw_mode in GATEWAY_MODES else "unknown"

    operational = value.get("operational", _INVALID)
    if operational not in OPERATIONAL_VALUES:
        return None

    raw_features = value.get("features", _INVALID)
    if mode == "observe":
        if type(raw_features) is not dict:
            return None
        raw_listener = raw_features.get("listener", _INVALID)
        if (
            type(raw_listener) is not dict
            or raw_listener.get("enabled", _INVALID) is not False
            or raw_listener.get("configured", _INVALID) is not False
            or raw_listener.get("operational", _INVALID) != "disabled"
        ):
            return None

    features = _normalize_features(raw_features)
    if features is None:
        return None
    if mode == "observe":
        features["listener"] = {
            "support": features["listener"]["support"],
            "enabled": False,
            "configured": False,
            "operational": "disabled",
        }

    configured_count = _nullable_count(value.get("configuredProviderCount"))
    healthy_count = _nullable_count(value.get("healthyProviderCount"))
    if configured_count is _INVALID or healthy_count is _INVALID:
        return None
    if (
        type(configured_count) is int
        and type(healthy_count) is int
        and healthy_count > configured_count
    ):
        return None

    actions = _normalize_actions(value.get("actions", []))
    if actions is None:
        return None
    last_error = _normalize_last_error(value.get("lastError"))
    return {
        "mode": mode,
        "operational": operational,
        "features": features,
        "configuredProviderCount": configured_count,
        "healthyProviderCount": healthy_count,
        "actions": actions,
        "lastError": last_error,
    }


def _normalize_features(value: object) -> dict[str, dict[str, object]] | None:
    if type(value) is not dict:
        return None
    normalized: dict[str, dict[str, object]] = {}
    for feature_id in FEATURE_IDS:
        raw = value.get(feature_id, _INVALID)
        if type(raw) is not dict:
            return None
        support = raw.get("support", _INVALID)
        enabled = raw.get("enabled", _INVALID)
        configured = raw.get("configured", _INVALID)
        operational = raw.get("operational", _INVALID)
        if (
            support not in SUPPORT_VALUES
            or not _is_known_boolean(enabled)
            or not _is_known_boolean(configured)
            or operational not in OPERATIONAL_VALUES
            or (
                operational == "ready"
                and (
                    enabled is False
                    or configured is False
                    or support == "unsupported"
                )
            )
        ):
            return None
        if support == "unsupported":
            normalized[feature_id] = {
                "support": "unsupported",
                "enabled": "unknown",
                "configured": "unknown",
                "operational": "unknown",
            }
            continue
        normalized[feature_id] = {
            "support": support,
            "enabled": enabled,
            "configured": configured,
            "operational": operational,
        }
    return normalized


def _normalize_actions(value: object) -> list[str] | None:
    if type(value) is not list:
        return None
    advertised = {
        item for item in value if type(item) is str and item in ACTION_IDS
    }
    return [action_id for action_id in ACTION_IDS if action_id in advertised]


def _normalize_last_error(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if type(value) is not dict:
        return _invalid_error()
    code = value.get("code")
    retryable = value.get("retryable")
    if code not in SAFE_ERROR_CODES or type(retryable) is not bool:
        return _invalid_error()
    result: dict[str, object] = {"code": code, "retryable": retryable}
    if "observedAt" in value:
        observed_at = _nullable_timestamp(value["observedAt"])
        if observed_at is None:
            return _invalid_error()
        result["observedAt"] = observed_at
    return result


def _nullable_count(value: object) -> int | None | object:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= _MAX_SAFE_INTEGER:
        return _INVALID
    return value


def _nullable_timestamp(value: object) -> str | None:
    if value is None or type(value) is not str or not _TIMESTAMP.fullmatch(value):
        return None
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return value


def _is_known_boolean(value: object) -> bool:
    return type(value) is bool or value == "unknown"


def _unknown_observer() -> dict[str, object]:
    return {
        "operational": "unknown",
        "generatedAt": None,
        "lastGoodAt": None,
        "dataRevision": None,
        "schemaVersion": None,
    }


def _unknown_gateway() -> dict[str, object]:
    return {
        "mode": "unknown",
        "operational": "unknown",
        "features": {
            feature_id: {
                "support": "unknown",
                "enabled": "unknown",
                "configured": "unknown",
                "operational": "unknown",
            }
            for feature_id in FEATURE_IDS
        },
        "configuredProviderCount": None,
        "healthyProviderCount": None,
        "actions": ["retry"],
        "lastError": _invalid_error(),
    }


def _invalid_error() -> dict[str, object]:
    return {"code": "capability_invalid", "retryable": False}


def _is_bounded_json(value: object) -> bool:
    counter = [0]
    if not _has_bounded_shape(value, depth=0, counter=counter):
        return False
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        return False
    return len(encoded) <= SERIALIZED_UTF8_LIMIT


def _has_bounded_shape(
    value: object,
    *,
    depth: int,
    counter: list[int],
) -> bool:
    counter[0] += 1
    if counter[0] > _MAX_INPUT_NODES or depth > _MAX_INPUT_DEPTH:
        return False
    if value is None or type(value) in (bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is str:
        return _is_bounded_text(value)
    if type(value) is list:
        return all(
            _has_bounded_shape(item, depth=depth + 1, counter=counter)
            for item in value
        )
    if type(value) is dict:
        return all(
            type(key) is str
            and _is_bounded_text(key)
            and _has_bounded_shape(item, depth=depth + 1, counter=counter)
            for key, item in value.items()
        )
    return False


def _is_bounded_text(value: str) -> bool:
    return (
        len(value) <= MAX_TEXT_LENGTH
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )
