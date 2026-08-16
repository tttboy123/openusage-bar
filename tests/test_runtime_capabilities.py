from __future__ import annotations

import copy
import importlib
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "openusage_bar/resources/runtime-capability-v1.schema.json"
API_VERSION = "runtime-capability.openusage/v1"
OBJECT_TYPE = "runtime.capability"
SERIALIZED_UTF8_LIMIT = 64 * 1024
MAX_SAFE_INTEGER = 9_007_199_254_740_991
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
_UNSET = object()

try:
    _CAPABILITIES = importlib.import_module("openusage_bar.runtime_capabilities")
except Exception as error:  # The explicit bootstrap test reports the cause.
    _CAPABILITIES = None
    _IMPORT_ERROR: Exception | None = error
else:
    _IMPORT_ERROR = None

_BUILD = getattr(_CAPABILITIES, "build_runtime_capability", None)
_VALIDATE = getattr(_CAPABILITIES, "validate_runtime_capability", None)
_CONTRACT_AVAILABLE = (
    callable(_BUILD) and callable(_VALIDATE) and SCHEMA_PATH.is_file()
)


def _feature(
    support: str = "supported",
    enabled: bool | str = True,
    configured: bool | str = True,
    operational: str = "ready",
) -> dict[str, object]:
    return {
        "support": support,
        "enabled": enabled,
        "configured": configured,
        "operational": operational,
    }


def _observer() -> dict[str, object]:
    return {
        "operational": "ready",
        "generatedAt": "2026-08-08T15:00:00Z",
        "lastGoodAt": "2026-08-08T14:59:00Z",
        "dataRevision": 42,
        "schemaVersion": "openusage/v1",
    }


def _features() -> dict[str, dict[str, object]]:
    return {
        "listener": _feature(),
        "should_send": _feature(operational="starting"),
        "responses": _feature(configured=False, operational="starting"),
        "cache": _feature(enabled=False, operational="disabled"),
        "fallback": _feature("unknown", "unknown", "unknown", "unknown"),
        "pii_redaction": _feature(operational="degraded"),
        # dispatch_events and adapter presence are deliberately not readiness.
        "streaming": _feature(operational="degraded"),
    }


def _gateway() -> dict[str, object]:
    return {
        "mode": "gateway",
        "operational": "degraded",
        "features": _features(),
        "configuredProviderCount": 2,
        "healthyProviderCount": 1,
        "actions": [
            "clear_cache",
            "learn_more",
            "retry",
            "open_settings",
            "disable",
            "enable",
            "retry",
            "unknown_action",
        ],
        "lastError": {
            "code": "provider_unavailable",
            "retryable": True,
            "observedAt": "2026-08-08T14:58:00Z",
        },
        "adapterCount": 5,
        "dispatchEvents": True,
    }


def _unknown_feature() -> dict[str, object]:
    return _feature("unknown", "unknown", "unknown", "unknown")


def _unknown_gateway() -> dict[str, object]:
    return {
        "mode": "unknown",
        "operational": "unknown",
        "features": {name: _unknown_feature() for name in FEATURE_IDS},
        "configuredProviderCount": None,
        "healthyProviderCount": None,
        "actions": ["retry"],
        "lastError": {
            "code": "capability_invalid",
            "retryable": False,
        },
    }


def _observe_gateway(listener_support: str = "supported") -> dict[str, object]:
    features = {name: _unknown_feature() for name in FEATURE_IDS}
    features["listener"] = _feature(
        support=listener_support,
        enabled=False,
        configured=False,
        operational="disabled",
    )
    return {
        "mode": "observe",
        "operational": "disabled",
        "features": features,
        "configuredProviderCount": None,
        "healthyProviderCount": None,
        "actions": ["retry"],
        "lastError": None,
    }


def _expected_observer() -> dict[str, object]:
    return copy.deepcopy(_observer())


def _build(
    observer: object = _UNSET,
    gateway: object = _UNSET,
    *,
    platform: str = "darwin",
) -> dict[str, object]:
    assert callable(_BUILD)
    return _BUILD(
        _observer() if observer is _UNSET else observer,
        _gateway() if gateway is _UNSET else gateway,
        platform=platform,
    )


def _validate(value: object) -> dict[str, object] | None:
    assert callable(_VALIDATE)
    return _VALIDATE(value)


def _resolve_local_ref(
    schema: dict[str, object],
    root: dict[str, object],
) -> dict[str, object]:
    reference = schema.get("$ref")
    if not isinstance(reference, str):
        return schema
    if not reference.startswith("#/"):
        raise AssertionError(f"schema oracle only permits local refs: {reference}")
    resolved: object = root
    for token in reference[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if type(resolved) is not dict or token not in resolved:
            raise AssertionError(f"unresolvable schema ref: {reference}")
        resolved = resolved[token]
    if type(resolved) is not dict:
        raise AssertionError(f"schema ref is not an object: {reference}")
    return resolved


def _object_schema(schema: object, root: dict[str, object]) -> dict[str, object]:
    if type(schema) is not dict:
        raise AssertionError("schema node must be an object")
    resolved = _resolve_local_ref(schema, root)
    node_type = resolved.get("type")
    if node_type == "object" or (
        type(node_type) is list and "object" in node_type
    ):
        return resolved
    for keyword in ("oneOf", "anyOf"):
        variants = resolved.get(keyword)
        if type(variants) is list:
            for variant in variants:
                try:
                    return _object_schema(variant, root)
                except AssertionError:
                    continue
    raise AssertionError("schema node has no object branch")


def _schema_type_matches(value: object, expected: object) -> bool:
    if type(expected) is list:
        return any(_schema_type_matches(value, item) for item in expected)
    return {
        "null": value is None,
        "boolean": type(value) is bool,
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "string": type(value) is str,
        "array": type(value) is list,
        "object": type(value) is dict,
    }.get(expected, False)


def _schema_accepts(
    value: object,
    schema: object,
    root: dict[str, object],
) -> bool:
    if type(schema) is not dict:
        return False
    if "$ref" in schema:
        resolved = _resolve_local_ref(schema, root)
        if not _schema_accepts(value, resolved, root):
            return False
        schema = {key: item for key, item in schema.items() if key != "$ref"}

    if "allOf" in schema and not all(
        _schema_accepts(value, item, root) for item in schema["allOf"]
    ):
        return False
    if "anyOf" in schema and not any(
        _schema_accepts(value, item, root) for item in schema["anyOf"]
    ):
        return False
    if "oneOf" in schema and sum(
        _schema_accepts(value, item, root) for item in schema["oneOf"]
    ) != 1:
        return False
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and not any(
        type(value) is type(item) and value == item for item in schema["enum"]
    ):
        return False

    expected_type = schema.get("type")
    if expected_type is not None and not _schema_type_matches(value, expected_type):
        return False

    if type(value) is dict:
        required = schema.get("required", [])
        if type(required) is not list or not all(key in value for key in required):
            return False
        properties = schema.get("properties", {})
        if type(properties) is not dict:
            return False
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in value
        ):
            return False
        for key, item in value.items():
            if key in properties and not _schema_accepts(item, properties[key], root):
                return False

    if type(value) is list:
        if "minItems" in schema and len(value) < schema["minItems"]:
            return False
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return False
        if schema.get("uniqueItems") is True:
            encoded = [json.dumps(item, sort_keys=True) for item in value]
            if len(encoded) != len(set(encoded)):
                return False
        items = schema.get("items")
        if items is not None and not all(
            _schema_accepts(item, items, root) for item in value
        ):
            return False

    if type(value) is str:
        if "minLength" in schema and len(value) < schema["minLength"]:
            return False
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return False
        pattern = schema.get("pattern")
        if pattern is not None and (
            type(pattern) is not str or re.fullmatch(pattern, value) is None
        ):
            return False
    if type(value) in (int, float):
        if "minimum" in schema and value < schema["minimum"]:
            return False
        if "maximum" in schema and value > schema["maximum"]:
            return False
    return True


def _assert_closed_object_schema(
    test: unittest.TestCase,
    schema: object,
    root: dict[str, object],
    required: tuple[str, ...],
    properties: tuple[str, ...] | None = None,
) -> dict[str, object]:
    node = _object_schema(schema, root)
    test.assertIs(node.get("additionalProperties"), False)
    test.assertEqual(set(node.get("required", [])), set(required))
    property_schemas = node.get("properties")
    test.assertIs(type(property_schemas), dict)
    test.assertEqual(set(property_schemas), set(properties or required))
    return node


class RuntimeCapabilityBootstrapTests(unittest.TestCase):
    def test_public_producer_and_validator_module_exists(self) -> None:
        self.assertIsNone(
            _IMPORT_ERROR,
            "missing openusage_bar.runtime_capabilities with "
            "build_runtime_capability/validate_runtime_capability: "
            f"{_IMPORT_ERROR}",
        )
        self.assertTrue(callable(_BUILD), "build_runtime_capability is missing")
        self.assertTrue(callable(_VALIDATE), "validate_runtime_capability is missing")

    def test_machine_readable_schema_freezes_the_renderer_envelope(self) -> None:
        self.assertTrue(
            SCHEMA_PATH.is_file(),
            f"missing renderer capability schema: {SCHEMA_PATH}",
        )
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            schema.get("$schema"), "https://json-schema.org/draft/2020-12/schema"
        )
        self.assertEqual(
            schema.get("$id"),
            "https://openusage.bar/schemas/runtime-capability-v1.schema.json",
        )
        root = _assert_closed_object_schema(
            self,
            schema,
            schema,
            ("apiVersion", "object", "observer", "gateway"),
        )
        properties = root["properties"]
        self.assertEqual(properties["apiVersion"].get("const"), API_VERSION)
        self.assertEqual(properties["object"].get("const"), OBJECT_TYPE)

        observer = _assert_closed_object_schema(
            self,
            properties["observer"],
            schema,
            (
                "operational",
                "generatedAt",
                "lastGoodAt",
                "dataRevision",
                "schemaVersion",
            ),
        )
        gateway = _assert_closed_object_schema(
            self,
            properties["gateway"],
            schema,
            (
                "mode",
                "operational",
                "features",
                "configuredProviderCount",
                "healthyProviderCount",
                "actions",
                "lastError",
            ),
        )
        features = _assert_closed_object_schema(
            self,
            gateway["properties"]["features"],
            schema,
            FEATURE_IDS,
        )
        for feature_id in FEATURE_IDS:
            with self.subTest(feature_schema=feature_id):
                feature = _assert_closed_object_schema(
                    self,
                    features["properties"][feature_id],
                    schema,
                    ("support", "enabled", "configured", "operational"),
                )
                feature_properties = feature["properties"]
                support = _resolve_local_ref(feature_properties["support"], schema)
                operational = _resolve_local_ref(
                    feature_properties["operational"], schema
                )
                self.assertEqual(set(support.get("enum", [])), set(SUPPORT_VALUES))
                self.assertEqual(
                    set(operational.get("enum", [])), set(OPERATIONAL_VALUES)
                )
                for field in ("enabled", "configured"):
                    fact = feature_properties[field]
                    for allowed in (True, False, "unknown"):
                        self.assertTrue(_schema_accepts(allowed, fact, schema))
                    for forbidden in (None, 0, 1, "true"):
                        self.assertFalse(_schema_accepts(forbidden, fact, schema))

        observer_operational = _resolve_local_ref(
            observer["properties"]["operational"], schema
        )
        gateway_mode = _resolve_local_ref(gateway["properties"]["mode"], schema)
        gateway_operational = _resolve_local_ref(
            gateway["properties"]["operational"], schema
        )
        self.assertEqual(
            set(observer_operational.get("enum", [])), set(OPERATIONAL_VALUES)
        )
        timestamp = observer["properties"]["generatedAt"]
        self.assertTrue(
            _schema_accepts("2026-08-08T15:00:00Z", timestamp, schema)
        )
        self.assertFalse(
            _schema_accepts("2026-08-08 15:00:00Z", timestamp, schema)
        )
        schema_version = observer["properties"]["schemaVersion"]
        self.assertTrue(_schema_accepts("openusage/v1", schema_version, schema))
        self.assertFalse(
            _schema_accepts("openusage/v1-private", schema_version, schema)
        )
        self.assertEqual(set(gateway_mode.get("enum", [])), set(GATEWAY_MODES))
        self.assertEqual(
            set(gateway_operational.get("enum", [])), set(OPERATIONAL_VALUES)
        )

        count_fields = {
            "dataRevision": observer["properties"]["dataRevision"],
            "configuredProviderCount": gateway["properties"][
                "configuredProviderCount"
            ],
            "healthyProviderCount": gateway["properties"]["healthyProviderCount"],
        }
        for field, count in count_fields.items():
            count_schema = _resolve_local_ref(count, schema)
            self.assertEqual(
                count_schema.get("maximum"), MAX_SAFE_INTEGER, field
            )
            for allowed in (None, 0, 2, MAX_SAFE_INTEGER):
                self.assertTrue(_schema_accepts(allowed, count, schema))
            for forbidden in (-1, True, 1.5, "2", MAX_SAFE_INTEGER + 1):
                self.assertFalse(_schema_accepts(forbidden, count, schema))

        actions = _resolve_local_ref(gateway["properties"]["actions"], schema)
        action_items = _resolve_local_ref(actions["items"], schema)
        self.assertIs(actions.get("uniqueItems"), True)
        self.assertEqual(actions.get("maxItems"), len(ACTION_IDS))
        self.assertEqual(set(action_items.get("enum", [])), set(ACTION_IDS))

        last_error_property = gateway["properties"]["lastError"]
        self.assertTrue(_schema_accepts(None, last_error_property, schema))
        last_error = _assert_closed_object_schema(
            self,
            last_error_property,
            schema,
            ("code", "retryable"),
            ("code", "retryable", "observedAt"),
        )
        error_code = _resolve_local_ref(last_error["properties"]["code"], schema)
        self.assertEqual(set(error_code.get("enum", [])), set(SAFE_ERROR_CODES))

    def test_schema_timestamp_fields_enforce_gregorian_calendar(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        observer = _object_schema(schema["properties"]["observer"], schema)
        gateway = _object_schema(schema["properties"]["gateway"], schema)
        last_error = _object_schema(
            gateway["properties"]["lastError"], schema
        )
        timestamp_fields = {
            "observer.generatedAt": observer["properties"]["generatedAt"],
            "observer.lastGoodAt": observer["properties"]["lastGoodAt"],
            "gateway.lastError.observedAt": last_error["properties"]["observedAt"],
        }
        valid_timestamps = {
            "minimum-year": "0001-01-01T00:00:00Z",
            "single-digit-millennium-leap-day": "0004-02-29T00:00:00Z",
            "century-leap-day": "2000-02-29T00:00:00Z",
        }
        invalid_timestamps = {
            "year-zero": "0000-01-01T00:00:00Z",
            "non-leap-century": "1900-02-29T00:00:00Z",
            "non-leap-year": "2026-02-29T00:00:00Z",
            "april-31": "2026-04-31T00:00:00Z",
            "month-13": "2026-13-01T00:00:00Z",
            "hour-24": "2026-01-01T24:00:00Z",
            "minute-60": "2026-01-01T00:60:00Z",
            "second-60": "2026-01-01T00:00:60Z",
        }

        for field, field_schema in timestamp_fields.items():
            for case, timestamp in valid_timestamps.items():
                with self.subTest(field=field, valid=case):
                    self.assertTrue(
                        _schema_accepts(timestamp, field_schema, schema)
                    )
            for case, timestamp in invalid_timestamps.items():
                with self.subTest(field=field, invalid=case):
                    self.assertFalse(
                        _schema_accepts(timestamp, field_schema, schema)
                    )


@unittest.skipUnless(
    _CONTRACT_AVAILABLE,
    "runtime capability producer/schema have not been implemented yet",
)
class RuntimeCapabilityContractTests(unittest.TestCase):
    def test_observe_listener_accepts_explicit_disabled_facts_for_every_support(
        self,
    ) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        for support in SUPPORT_VALUES:
            gateway = _observe_gateway(support)
            snapshot = _build(gateway=gateway)
            listener = snapshot["gateway"]["features"]["listener"]

            with self.subTest(support=support):
                self.assertEqual(snapshot["gateway"]["mode"], "observe")
                self.assertEqual(listener["support"], support)
                self.assertEqual(
                    listener,
                    {
                        "support": support,
                        "enabled": False,
                        "configured": False,
                        "operational": "disabled",
                    },
                )
                self.assertTrue(_schema_accepts(snapshot, schema, schema))
                self.assertEqual(_validate(snapshot), snapshot)

    def test_observe_listener_rejects_every_non_disabled_fact_combination(
        self,
    ) -> None:
        contradictions = (
            ("enabled-true", "enabled", True),
            ("enabled-unknown", "enabled", "unknown"),
            ("configured-true", "configured", True),
            ("configured-unknown", "configured", "unknown"),
            ("operational-starting", "operational", "starting"),
            ("operational-ready", "operational", "ready"),
            ("operational-degraded", "operational", "degraded"),
            ("operational-unavailable", "operational", "unavailable"),
            ("operational-unknown", "operational", "unknown"),
        )
        observer = _observer()

        for support in SUPPORT_VALUES:
            for name, field, value in contradictions:
                gateway = _observe_gateway(support)
                gateway["features"]["listener"][field] = value
                envelope = {
                    "apiVersion": API_VERSION,
                    "object": OBJECT_TYPE,
                    "observer": copy.deepcopy(observer),
                    "gateway": copy.deepcopy(gateway),
                }

                with self.subTest(path="build", support=support, case=name):
                    built = _build(observer=observer, gateway=gateway)
                    self.assertEqual(built["observer"], observer)
                    self.assertEqual(built["gateway"], _unknown_gateway())

                with self.subTest(path="validate", support=support, case=name):
                    normalized = _validate(envelope)
                    self.assertIsNotNone(normalized)
                    self.assertEqual(normalized["observer"], observer)
                    self.assertEqual(normalized["gateway"], _unknown_gateway())

    def test_schema_version_matches_the_64_character_schema_boundary(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        observer_schema = _object_schema(
            schema["properties"]["observer"], schema
        )
        version_schema = _resolve_local_ref(
            observer_schema["properties"]["schemaVersion"], schema
        )
        string_variant = next(
            variant
            for variant in version_schema["oneOf"]
            if variant.get("type") == "string"
        )
        self.assertEqual(string_variant["maxLength"], 64)

        at_limit = "openusage/v" + "1" * 53
        too_long = at_limit + "1"
        self.assertEqual(len(at_limit), 64)
        self.assertEqual(len(too_long), 65)

        observer = _observer()
        observer["schemaVersion"] = at_limit
        snapshot = _build(observer=observer)
        self.assertEqual(snapshot["observer"]["schemaVersion"], at_limit)
        self.assertTrue(_schema_accepts(snapshot, schema, schema))

        observer["schemaVersion"] = too_long
        snapshot = _build(observer=observer)
        self.assertIsNone(snapshot["observer"]["schemaVersion"])
        self.assertTrue(_schema_accepts(snapshot, schema, schema))

        envelope = _build()
        envelope["observer"]["schemaVersion"] = too_long
        normalized = _validate(envelope)
        self.assertIsNotNone(normalized)
        self.assertIsNone(normalized["observer"]["schemaVersion"])
        self.assertTrue(_schema_accepts(normalized, schema, schema))

    def test_bounded_text_uses_unicode_code_points_and_rejects_surrogates(
        self,
    ) -> None:
        baseline = {
            "apiVersion": API_VERSION,
            "object": OBJECT_TYPE,
            "observer": _observer(),
            "gateway": _gateway(),
        }

        at_limit = copy.deepcopy(baseline)
        at_limit["futureText"] = "\U0001F680" * 256
        accepted = _validate(at_limit)
        self.assertEqual(accepted, _build())
        self.assertNotIn("\U0001F680", json.dumps(accepted, ensure_ascii=False))

        over_limit = copy.deepcopy(baseline)
        over_limit["futureText"] = "\U0001F680" * 257
        self.assertIsNone(_validate(over_limit))

        for name, surrogate in (("high", "\ud800"), ("low", "\udc00")):
            with self.subTest(surrogate=name):
                input_envelope = copy.deepcopy(baseline)
                input_envelope["futureText"] = surrogate
                self.assertIsNone(_validate(input_envelope))

    def test_envelope_modes_and_seven_feature_facts_are_explicit(self) -> None:
        snapshot = _build()
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertTrue(
            _schema_accepts(snapshot, schema, schema),
            "build_runtime_capability output must satisfy the bundled schema",
        )
        self.assertEqual(snapshot["apiVersion"], API_VERSION)
        self.assertEqual(snapshot["object"], OBJECT_TYPE)
        self.assertEqual(snapshot["observer"], _expected_observer())
        gateway = snapshot["gateway"]
        self.assertEqual(gateway["mode"], "gateway")
        self.assertEqual(gateway["operational"], "degraded")
        self.assertEqual(set(gateway["features"]), set(FEATURE_IDS))
        self.assertEqual(gateway["features"], _features())

        mode_gateway = copy.deepcopy(_gateway())
        mode_gateway["operational"] = "unknown"
        mode_gateway["features"] = {
            name: _unknown_feature() for name in FEATURE_IDS
        }
        for raw_mode, expected in (
            ("observe", "observe"),
            ("advise", "advise"),
            ("gateway", "gateway"),
            ("unknown", "unknown"),
            ("future-mode", "unknown"),
        ):
            with self.subTest(mode=raw_mode):
                candidate = copy.deepcopy(mode_gateway)
                candidate["mode"] = raw_mode
                if raw_mode == "observe":
                    candidate["features"]["listener"] = _feature(
                        enabled=False,
                        configured=False,
                        operational="disabled",
                    )
                self.assertEqual(
                    _build(gateway=candidate)["gateway"]["mode"], expected
                )

        invalid_observer = _observer()
        invalid_observer["generatedAt"] = "2026-08-08 15:00:00Z"
        invalid_observer["schemaVersion"] = "openusage/v1-private"
        normalized_observer = _build(observer=invalid_observer)["observer"]
        self.assertIsNone(normalized_observer["generatedAt"])
        self.assertIsNone(normalized_observer["schemaVersion"])

        invalid_envelope = copy.deepcopy(snapshot)
        invalid_envelope["observer"] = invalid_observer
        validated_observer = _validate(invalid_envelope)["observer"]
        self.assertIsNone(validated_observer["generatedAt"])
        self.assertIsNone(validated_observer["schemaVersion"])

        max_revision = _observer()
        max_revision["dataRevision"] = MAX_SAFE_INTEGER
        self.assertEqual(
            _build(observer=max_revision)["observer"]["dataRevision"],
            MAX_SAFE_INTEGER,
        )
        too_large_revision = _observer()
        too_large_revision["dataRevision"] = MAX_SAFE_INTEGER + 1
        self.assertIsNone(
            _build(observer=too_large_revision)["observer"]["dataRevision"]
        )
        invalid_revision_envelope = copy.deepcopy(snapshot)
        invalid_revision_envelope["observer"] = too_large_revision
        self.assertIsNone(
            _validate(invalid_revision_envelope)["observer"]["dataRevision"]
        )

    def test_unsupported_feature_cannot_promote_runtime_claims(self) -> None:
        baseline = _build()
        safe_unsupported = {
            "support": "unsupported",
            "enabled": "unknown",
            "configured": "unknown",
            "operational": "unknown",
        }
        for operational in ("starting", "degraded", "unavailable"):
            raw_gateway = _gateway()
            raw_gateway["features"]["listener"] = _feature(
                support="unsupported",
                enabled=True,
                configured=True,
                operational=operational,
            )
            expected = copy.deepcopy(baseline)
            expected["gateway"]["features"]["listener"] = safe_unsupported

            with self.subTest(path="build", operational=operational):
                self.assertEqual(_build(gateway=raw_gateway), expected)

            raw_envelope = copy.deepcopy(baseline)
            raw_envelope["gateway"]["features"]["listener"] = copy.deepcopy(
                raw_gateway["features"]["listener"]
            )
            with self.subTest(path="validate", operational=operational):
                self.assertEqual(_validate(raw_envelope), expected)

    def test_counts_actions_errors_and_streaming_are_never_inferred(self) -> None:
        snapshot = _build()
        gateway = snapshot["gateway"]
        self.assertEqual(gateway["configuredProviderCount"], 2)
        self.assertEqual(gateway["healthyProviderCount"], 1)
        self.assertNotIn("adapterCount", gateway)
        self.assertNotIn("dispatchEvents", gateway)
        self.assertEqual(
            gateway["features"]["streaming"]["operational"], "degraded"
        )
        self.assertEqual(gateway["actions"], list(ACTION_IDS))
        self.assertEqual(
            gateway["lastError"],
            {
                "code": "provider_unavailable",
                "retryable": True,
                "observedAt": "2026-08-08T14:58:00Z",
            },
        )

        subset = copy.deepcopy(_gateway())
        subset["actions"] = [
            "learn_more",
            "retry",
            "unknown_action",
            "open_settings",
            "retry",
        ]
        self.assertEqual(
            _build(gateway=subset)["gateway"]["actions"],
            ["open_settings", "retry", "learn_more"],
        )
        subset_envelope = copy.deepcopy(snapshot)
        subset_envelope["gateway"]["actions"] = copy.deepcopy(subset["actions"])
        self.assertEqual(
            _validate(subset_envelope)["gateway"]["actions"],
            ["open_settings", "retry", "learn_more"],
        )
        no_actions = copy.deepcopy(_gateway())
        no_actions["actions"] = []
        self.assertEqual(_build(gateway=no_actions)["gateway"]["actions"], [])
        no_action_envelope = copy.deepcopy(snapshot)
        no_action_envelope["gateway"]["actions"] = []
        self.assertEqual(
            _validate(no_action_envelope)["gateway"]["actions"], []
        )

        no_counts = copy.deepcopy(_gateway())
        no_counts.pop("configuredProviderCount")
        no_counts.pop("healthyProviderCount")
        normalized = _build(gateway=no_counts)["gateway"]
        self.assertIsNone(normalized["configuredProviderCount"])
        self.assertIsNone(normalized["healthyProviderCount"])

        for code in SAFE_ERROR_CODES:
            with self.subTest(error_code=code):
                raw = copy.deepcopy(_gateway())
                raw["lastError"] = {"code": code, "retryable": True}
                self.assertEqual(
                    _build(gateway=raw)["gateway"]["lastError"]["code"], code
                )
        raw = copy.deepcopy(_gateway())
        raw["lastError"] = {
            "code": "secret_upstream_error",
            "retryable": True,
            "message": "must-not-reflect",
        }
        self.assertEqual(
            _build(gateway=raw)["gateway"]["lastError"],
            {"code": "capability_invalid", "retryable": False},
        )

    def test_malformed_or_oversized_gateway_preserves_observer(self) -> None:
        base = _build()
        observer = base["observer"]
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

        unknown_feature_enum = copy.deepcopy(_gateway())
        unknown_feature_enum["features"]["listener"]["operational"] = "future"
        negative_count = copy.deepcopy(_gateway())
        negative_count["configuredProviderCount"] = -1
        non_integer_count = copy.deepcopy(_gateway())
        non_integer_count["healthyProviderCount"] = "1"
        impossible_counts = copy.deepcopy(_gateway())
        impossible_counts["healthyProviderCount"] = 3
        configured_too_large = copy.deepcopy(_gateway())
        configured_too_large["configuredProviderCount"] = MAX_SAFE_INTEGER + 1
        healthy_too_large = copy.deepcopy(_gateway())
        healthy_too_large["configuredProviderCount"] = None
        healthy_too_large["healthyProviderCount"] = MAX_SAFE_INTEGER + 1
        contradictory = copy.deepcopy(_gateway())
        contradictory["mode"] = "observe"
        contradictory["features"]["listener"] = _feature(
            enabled=False,
            configured=False,
            operational="ready",
        )
        oversized = copy.deepcopy(_gateway())
        oversized["oversized"] = "界" * 22_000
        self.assertGreater(_serialized_utf8_size(oversized), SERIALIZED_UTF8_LIMIT)
        too_deep = copy.deepcopy(_gateway())
        too_deep["deep"] = _deep_value(20)

        malformed_values = [
            ("missing", None),
            ("malformed-features", {"mode": "gateway", "features": []}),
            ("unknown-feature-enum", unknown_feature_enum),
            ("negative-count", negative_count),
            ("non-integer-count", non_integer_count),
            ("healthy-exceeds-configured", impossible_counts),
            ("configured-count-exceeds-js-safe-integer", configured_too_large),
            ("healthy-count-exceeds-js-safe-integer", healthy_too_large),
            ("contradictory-facts", contradictory),
            ("oversized-utf8", oversized),
            ("too-deep", too_deep),
        ]
        for name, gateway in malformed_values:
            built = _build(
                observer=copy.deepcopy(observer),
                gateway=copy.deepcopy(gateway),
            )
            raw = copy.deepcopy(base)
            raw["gateway"] = copy.deepcopy(gateway)
            with self.subTest(case=name):
                self.assertEqual(built["observer"], observer)
                self.assertEqual(built["gateway"], _unknown_gateway())
                self.assertTrue(_schema_accepts(built, schema, schema))
                self.assertLessEqual(
                    _serialized_utf8_size(built), SERIALIZED_UTF8_LIMIT
                )
                normalized = _validate(raw)
                self.assertIsNotNone(normalized)
                self.assertEqual(normalized["observer"], observer)
                self.assertEqual(normalized["gateway"], _unknown_gateway())
                self.assertTrue(_schema_accepts(normalized, schema, schema))
                self.assertLessEqual(
                    _serialized_utf8_size(normalized), SERIALIZED_UTF8_LIMIT
                )

        additive = copy.deepcopy(base)
        additive["unknownTop"] = "ignored"
        additive["gateway"]["futureField"] = "ignored"
        additive["observer"]["futureField"] = "ignored"
        self.assertEqual(_validate(additive), base)
        self.assertIsNone(_validate(None))
        self.assertIsNone(_validate({**base, "apiVersion": "future/v9"}))
        self.assertIsNone(_validate({**base, "object": "runtime.private"}))

    def test_renderer_snapshot_drops_every_private_canary_and_is_bounded(self) -> None:
        observer = _observer()
        gateway = _gateway()
        canaries = {
            "host": "CANARY_HOST_9f31",
            "port": "CANARY_PORT_9f32",
            "socketPath": "CANARY_SOCKET_9f33",
            "tokenPath": "CANARY_TOKEN_PATH_9f34",
            "databasePath": "CANARY_DB_9f35",
            "homePath": "CANARY_HOME_9f36",
            "bearer": "CANARY_BEARER_9f37",
            "authorization": "CANARY_AUTH_9f38",
            "credential": "CANARY_CREDENTIAL_9f39",
            "accountRef": "CANARY_ACCOUNT_9f40",
            "rawError": "CANARY_RAW_ERROR_9f41",
            "prompt": "CANARY_PROMPT_9f42",
            "response": "CANARY_RESPONSE_9f43",
            "outputText": "CANARY_OUTPUT_9f44",
            "rawChunk": "CANARY_CHUNK_9f45",
            "transport": "CANARY_TRANSPORT_9f46",
        }
        gateway.update(canaries)
        gateway["features"]["listener"].update(canaries)
        gateway["lastError"].update(
            {"message": canaries["rawError"], "stack": "CANARY_STACK_9f47"}
        )
        observer.update(canaries)

        snapshot = _build(observer=observer, gateway=gateway)
        serialized = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
        for name, canary in canaries.items():
            with self.subTest(field=name):
                self.assertNotIn(canary, serialized)
        self.assertNotIn("CANARY_STACK_9f47", serialized)
        self.assertLessEqual(
            _serialized_utf8_size(snapshot), SERIALIZED_UTF8_LIMIT
        )
        self.assertLessEqual(_maximum_depth(snapshot), 6)
        self.assertTrue(
            all(len(text) <= 256 for text in _all_strings(snapshot))
        )
        self.assertEqual(
            set(snapshot), {"apiVersion", "object", "observer", "gateway"}
        )

    def test_three_platforms_produce_identical_transport_free_semantics(self) -> None:
        snapshots = {
            platform: _build(platform=platform)
            for platform in ("darwin", "win32", "linux")
        }
        self.assertEqual(snapshots["darwin"], snapshots["win32"])
        self.assertEqual(snapshots["darwin"], snapshots["linux"])
        serialized = json.dumps(list(snapshots.values()), sort_keys=True)
        for forbidden in (
            "platform",
            "darwin",
            "win32",
            "linux",
            "socket",
            "namedPipe",
            "localhost",
            "127.0.0.1",
        ):
            self.assertNotIn(forbidden, serialized)
        snapshot = snapshots["darwin"]
        self.assertEqual(_validate(snapshot), snapshot)


def _deep_value(depth: int) -> object:
    value: object = "leaf"
    for _ in range(depth):
        value = {"nested": value}
    return value


def _serialized_utf8_size(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _maximum_depth(value: object) -> int:
    if type(value) is dict:
        return 1 + max((_maximum_depth(item) for item in value.values()), default=0)
    if type(value) is list:
        return 1 + max((_maximum_depth(item) for item in value), default=0)
    return 0


def _all_strings(value: object) -> list[str]:
    if type(value) is dict:
        return [
            text
            for key, item in value.items()
            for text in (str(key), *_all_strings(item))
        ]
    if type(value) is list:
        return [text for item in value for text in _all_strings(item)]
    return [value] if type(value) is str else []


if __name__ == "__main__":
    unittest.main()
