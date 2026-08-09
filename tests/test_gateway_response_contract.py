from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path
from typing import Any

from openusage_bar.gateway.api import MAX_BODY_BYTES, GatewayRouter
from openusage_bar.gateway.contracts import Decision, GatewayMode
from openusage_bar.gateway.fallback import (
    Candidate,
    FailureSignal,
    FallbackAction,
    FallbackSelector,
    ReplayState,
)
from openusage_bar.gateway.providers import GatewayProviderError, ProviderResult
from openusage_bar.gateway.runtime import GatewayRuntime


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "openusage_bar/resources/gateway-response-v1.schema.json"
API_VERSION = "gateway.openusage/v1"
TERMINAL_TYPES = frozenset(
    {
        "gateway.response.completed",
        "gateway.response.interrupted",
        "gateway.response.uncertain",
        "gateway.error",
    }
)
EXPECTED_EVENT_TYPES = frozenset(
    {
        "gateway.response.started",
        "gateway.output_text.delta",
        "gateway.usage.snapshot",
        "gateway.tool.observed",
        "gateway.cache.event",
        "gateway.fallback.event",
        *TERMINAL_TYPES,
    }
)


def _json_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


class _SchemaOracle:
    """Small stdlib validator for the closed subset used by this schema.

    The package intentionally does not depend on ``jsonschema``.  This oracle
    keeps the contract tests useful in the minimal package environment while
    an optional standards implementation performs the meta-schema check when
    it is available.
    """

    def __init__(self, root: dict[str, object]) -> None:
        self.root = root

    def accepts(
        self,
        instance: object,
        schema: dict[str, object] | bool | None = None,
    ) -> bool:
        return self._accepts(instance, self.root if schema is None else schema)

    def definition(self, name: str) -> dict[str, object]:
        definitions = self.root["$defs"]
        assert type(definitions) is dict
        definition = definitions[name]
        assert type(definition) is dict
        return definition

    def _accepts(
        self,
        instance: object,
        schema: dict[str, object] | bool,
    ) -> bool:
        if schema is True:
            return True
        if schema is False or type(schema) is not dict:
            return False

        reference = schema.get("$ref")
        if reference is not None:
            if type(reference) is not str or not reference.startswith("#/$defs/"):
                return False
            name = reference.removeprefix("#/$defs/").replace("~1", "/").replace(
                "~0", "~"
            )
            try:
                target = self.definition(name)
            except (AssertionError, KeyError):
                return False
            if not self._accepts(instance, target):
                return False

        all_of = schema.get("allOf")
        if all_of is not None and (
            type(all_of) is not list
            or not all(self._accepts(instance, child) for child in all_of)
        ):
            return False

        any_of = schema.get("anyOf")
        if any_of is not None and (
            type(any_of) is not list
            or not any(self._accepts(instance, child) for child in any_of)
        ):
            return False

        one_of = schema.get("oneOf")
        if one_of is not None and (
            type(one_of) is not list
            or sum(self._accepts(instance, child) for child in one_of) != 1
        ):
            return False

        negated = schema.get("not")
        if negated is not None and self._accepts(instance, negated):
            return False

        conditional = schema.get("if")
        if conditional is not None:
            branch = "then" if self._accepts(instance, conditional) else "else"
            selected = schema.get(branch)
            if selected is not None and not self._accepts(instance, selected):
                return False

        declared_type = schema.get("type")
        if declared_type is not None:
            choices = (
                declared_type if type(declared_type) is list else [declared_type]
            )
            if not any(self._matches_type(instance, choice) for choice in choices):
                return False

        if "const" in schema and not _json_equal(instance, schema["const"]):
            return False
        if "enum" in schema:
            values = schema["enum"]
            if type(values) is not list or not any(
                _json_equal(instance, item) for item in values
            ):
                return False

        if type(instance) is str:
            minimum = schema.get("minLength")
            maximum = schema.get("maxLength")
            maximum_utf8 = schema.get("x-openusage-maxUtf8Bytes")
            pattern = schema.get("pattern")
            if type(minimum) is int and len(instance) < minimum:
                return False
            if type(maximum) is int and len(instance) > maximum:
                return False
            if maximum_utf8 is not None:
                if type(maximum_utf8) is not int or maximum_utf8 < 0:
                    return False
                try:
                    if len(instance.encode("utf-8")) > maximum_utf8:
                        return False
                except UnicodeEncodeError:
                    return False
            if type(pattern) is str and re.search(pattern, instance) is None:
                return False

        if type(instance) in {int, float}:
            minimum = schema.get("minimum")
            maximum = schema.get("maximum")
            if type(minimum) in {int, float} and instance < minimum:
                return False
            if type(maximum) in {int, float} and instance > maximum:
                return False

        if type(instance) is dict:
            properties = schema.get("properties", {})
            if type(properties) is not dict:
                return False
            required = schema.get("required", [])
            if type(required) is not list or any(key not in instance for key in required):
                return False
            for key, value in instance.items():
                child = properties.get(key)
                if child is None:
                    if schema.get("additionalProperties") is False:
                        return False
                    additional = schema.get("additionalProperties")
                    if type(additional) is dict and not self._accepts(value, additional):
                        return False
                elif not self._accepts(value, child):
                    return False
            dependencies = schema.get("dependentRequired", {})
            if type(dependencies) is not dict:
                return False
            for key, dependent in dependencies.items():
                if key in instance and (
                    type(dependent) is not list
                    or any(name not in instance for name in dependent)
                ):
                    return False

        return True

    @staticmethod
    def _matches_type(instance: object, declared: object) -> bool:
        return {
            "null": instance is None,
            "boolean": type(instance) is bool,
            "integer": type(instance) is int,
            "number": type(instance) in {int, float},
            "string": type(instance) is str,
            "array": type(instance) is list,
            "object": type(instance) is dict,
        }.get(declared, False)


def _success(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "apiVersion": API_VERSION,
        "object": "gateway.response",
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "stream": False,
        "status": "complete",
        "outputText": "Hello.",
        "usage": {"inputTokens": 8, "outputTokens": 2},
        "tool": {"observed": False},
        "cache": {"outcome": "miss"},
        "fallback": {
            "attempted": False,
            "attemptCount": 1,
            "finalAction": "none",
        },
    }
    payload.update(overrides)
    return payload


def _error(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "apiVersion": API_VERSION,
        "object": "gateway.error",
        "error": {
            "code": "upstream_timeout",
            "message": "Provider request failed.",
            "retryable": True,
        },
    }
    payload.update(overrides)
    return payload


def _started(sequence: int = 0) -> dict[str, object]:
    return {
        "apiVersion": API_VERSION,
        "type": "gateway.response.started",
        "sequence": sequence,
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "stream": True,
    }


def _terminal(
    sequence: int,
    *,
    provider: str = "openai",
    model: str = "gpt-4.1-mini",
    status: str = "complete",
    cache: dict[str, object] | None = None,
    fallback: dict[str, object] | None = None,
    tool_observed: bool = False,
) -> dict[str, object]:
    suffix = {
        "complete": "completed",
        "interrupted": "interrupted",
        "uncertain": "uncertain",
    }[status]
    return {
        "apiVersion": API_VERSION,
        "type": f"gateway.response.{suffix}",
        "sequence": sequence,
        "provider": provider,
        "model": model,
        "status": status,
        "usage": {"inputTokens": 8, "outputTokens": 2},
        "tool": {"observed": tool_observed},
        "cache": cache or {"outcome": "miss"},
        "fallback": fallback
        or {"attempted": False, "attemptCount": 1, "finalAction": "none"},
    }


def _event_schema(root: dict[str, object]) -> dict[str, object]:
    return {
        "$schema": root["$schema"],
        "$defs": root["$defs"],
        "$ref": "#/$defs/sseEvent",
    }


def _parse_sse(raw: bytes) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for block in raw.split(b"\n\n"):
        if not block:
            continue
        lines = block.splitlines()
        if len(lines) != 2 or not lines[0].startswith(b"event: ") or not lines[
            1
        ].startswith(b"data: "):
            raise AssertionError(f"malformed Gateway SSE block: {block!r}")
        wire_name = lines[0].removeprefix(b"event: ").decode("ascii")
        data = json.loads(lines[1].removeprefix(b"data: ").decode("utf-8"))
        if type(data) is not dict:
            raise AssertionError("Gateway SSE data must be an object")
        events.append((wire_name, data))
    return events


def _sequence_violations(
    events: list[tuple[str, dict[str, object]]],
    oracle: _SchemaOracle,
    event_schema: dict[str, object],
    *,
    provider_side_effect_observed: bool = False,
    client_aborted: bool = False,
) -> list[str]:
    failures: list[str] = []
    terminal_indexes: list[int] = []
    output_visible = False
    tool_observed = False

    for index, (wire_name, data) in enumerate(events):
        event_type = data.get("type")
        if wire_name != event_type:
            failures.append(f"event {index}: wire event must equal data.type")
        if not oracle.accepts(data, event_schema):
            failures.append(f"event {index}: data does not match sseEvent schema")
        if data.get("sequence") != index:
            failures.append(f"event {index}: sequence must be {index}")
        if event_type in TERMINAL_TYPES:
            terminal_indexes.append(index)
        if terminal_indexes and terminal_indexes[0] < index:
            failures.append(f"event {index}: event appears after terminal")
        if event_type == "gateway.output_text.delta":
            output_visible = True
        if event_type == "gateway.tool.observed":
            tool_observed = True
        if event_type == "gateway.fallback.event":
            fallback = data.get("fallback")
            action = fallback.get("finalAction") if type(fallback) is dict else None
            if action in {"retry", "degrade_to_cheap"} and (
                output_visible
                or tool_observed
                or provider_side_effect_observed
                or client_aborted
            ):
                failures.append(f"event {index}: unsafe fallback replay")

    if not events:
        return ["stream has no events"]
    first_type = events[0][1].get("type")
    if first_type == "gateway.error":
        if len(events) != 1:
            failures.append("pre-accept error must be the only event")
    elif first_type != "gateway.response.started":
        failures.append("accepted stream must start with gateway.response.started")
    elif sum(data.get("type") == first_type for _, data in events) != 1:
        failures.append("gateway.response.started must occur exactly once")

    if not client_aborted and len(terminal_indexes) != 1:
        failures.append("writable stream must have exactly one terminal event")
    if len(terminal_indexes) > 1:
        failures.append("stream has more than one terminal event")

    terminal = events[terminal_indexes[-1]][1] if terminal_indexes else None
    cache_events = [
        data["cache"]
        for _, data in events
        if data.get("type") == "gateway.cache.event" and type(data.get("cache")) is dict
    ]
    if type(terminal) is dict and terminal.get("type") != "gateway.error":
        cache = terminal.get("cache")
        fallback = terminal.get("fallback")
        tool = terminal.get("tool")
        outcome = cache.get("outcome") if type(cache) is dict else None
        attempts = fallback.get("attemptCount") if type(fallback) is dict else None
        observed = tool.get("observed") if type(tool) is dict else None
        if outcome == "exact_hit" and attempts != 0:
            failures.append("exact cache hit must perform zero Provider calls")
        if outcome == "exact_hit" and observed is not False:
            failures.append("tool-observed output cannot be an exact cache hit")
        if outcome == "prefix_hint" and (type(attempts) is not int or attempts < 1):
            failures.append("prefix hint must still perform a Provider call")
        if cache_events and cache_events[-1] != cache:
            failures.append("terminal cache summary must be authoritative")

    return failures


class GatewayResponseSchemaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.oracle = _SchemaOracle(cls.schema)
        cls.sse_schema = _event_schema(cls.schema)

    def test_schema_is_draft_2020_12_meta_valid_without_a_runtime_dependency(
        self,
    ) -> None:
        self.assertEqual(
            self.schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
        )
        self.assertEqual(
            self.schema["$id"],
            "https://openusage.bar/schemas/gateway-response-v1.schema.json",
        )
        definitions = self.schema.get("$defs")
        self.assertIs(type(definitions), dict)

        unresolved: list[str] = []

        def visit(value: object) -> None:
            if type(value) is dict:
                reference = value.get("$ref")
                if type(reference) is str and reference.startswith("#/$defs/"):
                    name = reference.removeprefix("#/$defs/")
                    if name not in definitions:
                        unresolved.append(reference)
                for child in value.values():
                    visit(child)
            elif type(value) is list:
                for child in value:
                    visit(child)

        visit(self.schema)
        self.assertEqual(unresolved, [])

        try:
            from jsonschema import Draft202012Validator
        except ModuleNotFoundError:
            return
        Draft202012Validator.check_schema(self.schema)

    def test_bounded_json_success_and_error_objects_are_strict(self) -> None:
        valid = [
            _success(),
            _success(status="interrupted", usage=None),
            _success(status="uncertain", usage=None),
            _error(),
            _error(
                fallback={
                    "attempted": True,
                    "attemptCount": 1,
                    "finalAction": "fail",
                    "errorCode": "upstream_timeout",
                    "retryable": True,
                }
            ),
        ]
        for payload in valid:
            with self.subTest(valid=payload.get("object"), status=payload.get("status")):
                self.assertTrue(self.oracle.accepts(payload), payload)

        invalid: dict[str, dict[str, object]] = {}
        invalid["unknown top-level field"] = _success(debug="not public")
        invalid["unknown provider"] = _success(provider="private-provider")
        invalid["oversized model"] = _success(model="m" * 257)
        invalid["negative usage"] = _success(
            usage={"inputTokens": -1, "outputTokens": 2}
        )
        invalid["unknown nested field"] = _success(
            tool={"observed": False, "arguments": "not public"}
        )
        invalid["unbounded error message"] = _error(
            error={
                "code": "upstream_timeout",
                "message": "x" * 257,
                "retryable": True,
            }
        )
        invalid["control in error"] = _error(
            error={
                "code": "upstream_timeout",
                "message": "unsafe\nmessage",
                "retryable": True,
            }
        )
        invalid["partial fallback error pair"] = _error(
            fallback={
                "attempted": True,
                "attemptCount": 1,
                "finalAction": "fail",
                "errorCode": "upstream_timeout",
            }
        )
        for name, payload in invalid.items():
            with self.subTest(invalid=name):
                self.assertFalse(self.oracle.accepts(payload), payload)

    def test_cache_outcome_encodes_provider_call_and_replay_safety(self) -> None:
        exact = _success(
            stream=True,
            cache={"outcome": "exact_hit"},
            fallback={
                "attempted": False,
                "attemptCount": 0,
                "finalAction": "none",
            },
        )
        prefix = _success(
            stream=True,
            cache={"outcome": "prefix_hint", "hintDepth": 2},
        )
        self.assertTrue(self.oracle.accepts(exact), exact)
        self.assertTrue(self.oracle.accepts(prefix), prefix)

        exact_with_provider_call = copy.deepcopy(exact)
        exact_with_provider_call["fallback"]["attemptCount"] = 1
        exact_with_tool = copy.deepcopy(exact)
        exact_with_tool["tool"]["observed"] = True
        prefix_without_provider_call = copy.deepcopy(prefix)
        prefix_without_provider_call["fallback"]["attemptCount"] = 0

        unsafe = {
            "exact hit with Provider call": exact_with_provider_call,
            "exact hit with tool output": exact_with_tool,
            "prefix hint without Provider call": prefix_without_provider_call,
        }
        for name, payload in unsafe.items():
            with self.subTest(case=name):
                self.assertFalse(
                    self.oracle.accepts(payload),
                    f"Gateway schema accepted unsafe cache semantics: {name}",
                )

    def test_utf8_byte_limits_are_declared_and_match_runtime_boundaries(
        self,
    ) -> None:
        from openusage_bar.gateway.response import (
            gateway_events,
            validate_gateway_payload,
        )

        maximum_output_bytes = 16 * 1024 * 1024
        maximum_delta_bytes = 64 * 1024
        output_rule = self.oracle.definition("gatewayResponse")["properties"][
            "outputText"
        ]
        delta_rule = self.oracle.definition("outputTextDeltaEvent")["properties"][
            "text"
        ]
        with self.subTest(boundary="outputText schema extension"):
            self.assertEqual(
                output_rule.get("x-openusage-maxUtf8Bytes"),
                maximum_output_bytes,
            )
            self.assertNotIn("maxLength", output_rule)
        with self.subTest(boundary="delta schema extension"):
            self.assertEqual(
                delta_rule.get("x-openusage-maxUtf8Bytes"),
                maximum_delta_bytes,
            )
            self.assertNotIn("maxLength", delta_rule)

        exact_output = "🙂" * (maximum_output_bytes // 4)
        over_output = exact_output + "🙂"
        for name, text, expected in (
            ("exact output byte limit", exact_output, True),
            ("over output byte limit", over_output, False),
        ):
            payload = _success(outputText=text)
            schema_accepts = self.oracle.accepts(payload)
            runtime_accepts = validate_gateway_payload(payload) is not None
            with self.subTest(boundary=name):
                self.assertEqual(schema_accepts, expected)
                self.assertEqual(runtime_accepts, expected)
                self.assertEqual(schema_accepts, runtime_accepts)

        exact_delta_text = "🙂" * (maximum_delta_bytes // 4)
        over_delta_text = exact_delta_text + "🙂"
        exact_delta = {
            "apiVersion": API_VERSION,
            "type": "gateway.output_text.delta",
            "sequence": 1,
            "text": exact_delta_text,
        }
        over_delta = {**exact_delta, "text": over_delta_text}
        with self.subTest(boundary="exact delta byte limit"):
            self.assertTrue(self.oracle.accepts(exact_delta, self.sse_schema))
        with self.subTest(boundary="over delta byte limit"):
            self.assertFalse(self.oracle.accepts(over_delta, self.sse_schema))

        iterator = gateway_events(
            _success(stream=True, outputText=over_delta_text),
            accepted_provider="openai",
            accepted_model="gpt-4.1-mini",
        )
        self.assertIsNotNone(iterator)
        assert iterator is not None
        deltas = [
            event["text"]
            for event in iterator
            if event.get("type") == "gateway.output_text.delta"
        ]
        self.assertEqual("".join(deltas), over_delta_text)
        self.assertEqual(len(deltas), 2)
        self.assertTrue(
            all(len(text.encode("utf-8")) <= maximum_delta_bytes for text in deltas)
        )

    def test_sse_union_is_complete_namespaced_and_closed(self) -> None:
        union = self.oracle.definition("sseEvent")["oneOf"]
        self.assertIs(type(union), list)
        actual: set[str] = set()
        for branch in union:
            self.assertIs(type(branch), dict)
            reference = branch["$ref"]
            definition = self.oracle.definition(reference.rsplit("/", 1)[-1])
            event_type = definition["properties"]["type"]["const"]
            actual.add(event_type)
            self.assertTrue(event_type.startswith("gateway."))
            self.assertIs(definition.get("additionalProperties"), False)
        self.assertEqual(actual, EXPECTED_EVENT_TYPES)

        fallback = {
            "attempted": True,
            "attemptCount": 2,
            "finalAction": "retry",
            "errorCode": "upstream_timeout",
            "retryable": True,
        }
        examples = [
            _started(),
            {
                "apiVersion": API_VERSION,
                "type": "gateway.output_text.delta",
                "sequence": 1,
                "text": "Hello.",
            },
            {
                "apiVersion": API_VERSION,
                "type": "gateway.usage.snapshot",
                "sequence": 2,
                "usage": {"inputTokens": 8, "outputTokens": 2},
            },
            {
                "apiVersion": API_VERSION,
                "type": "gateway.tool.observed",
                "sequence": 2,
                "tool": {"observed": True},
            },
            {
                "apiVersion": API_VERSION,
                "type": "gateway.cache.event",
                "sequence": 2,
                "cache": {"outcome": "prefix_hint", "hintDepth": 2},
            },
            {
                "apiVersion": API_VERSION,
                "type": "gateway.fallback.event",
                "sequence": 2,
                "fallback": fallback,
            },
            _terminal(3, provider="ollama", model="qwen", fallback=fallback),
            _terminal(3, status="interrupted"),
            _terminal(3, status="uncertain"),
            {
                "apiVersion": API_VERSION,
                "type": "gateway.error",
                "sequence": 0,
                "error": {
                    "code": "invalid_request",
                    "message": "Invalid request.",
                    "retryable": False,
                },
            },
        ]
        for event in examples:
            with self.subTest(event=event["type"]):
                self.assertTrue(self.oracle.accepts(event, self.sse_schema), event)
                poisoned = {**event, "providerRequestId": "req_private"}
                self.assertFalse(self.oracle.accepts(poisoned, self.sse_schema))

    def test_public_schema_has_no_provider_native_or_private_field_names(self) -> None:
        property_names: set[str] = set()

        def visit(value: object) -> None:
            if type(value) is dict:
                properties = value.get("properties")
                if type(properties) is dict:
                    property_names.update(properties)
                for child in value.values():
                    visit(child)
            elif type(value) is list:
                for child in value:
                    visit(child)

        visit(self.schema)
        forbidden = {
            "authorization",
            "bearerToken",
            "chunk",
            "cookie",
            "credential",
            "credentials",
            "header",
            "headers",
            "path",
            "prompt",
            "providerHeaders",
            "providerRequestId",
            "rawChunk",
            "requestBody",
            "requestId",
            "responseBody",
            "responseId",
            "toolArguments",
            "toolCallId",
            "toolName",
        }
        self.assertEqual(property_names & forbidden, set())


class GatewayEventSequenceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.oracle = _SchemaOracle(cls.schema)
        cls.sse_schema = _event_schema(cls.schema)

    def test_exact_hit_and_prefix_hint_have_deterministic_sequences(self) -> None:
        exact_fallback = {
            "attempted": False,
            "attemptCount": 0,
            "finalAction": "none",
        }
        exact = [
            ("gateway.response.started", _started(0)),
            (
                "gateway.cache.event",
                {
                    "apiVersion": API_VERSION,
                    "type": "gateway.cache.event",
                    "sequence": 1,
                    "cache": {"outcome": "exact_hit"},
                },
            ),
            (
                "gateway.output_text.delta",
                {
                    "apiVersion": API_VERSION,
                    "type": "gateway.output_text.delta",
                    "sequence": 2,
                    "text": "",
                },
            ),
            (
                "gateway.response.completed",
                _terminal(
                    3,
                    cache={"outcome": "exact_hit"},
                    fallback=exact_fallback,
                ),
            ),
        ]
        prefix = [
            ("gateway.response.started", _started(0)),
            (
                "gateway.cache.event",
                {
                    "apiVersion": API_VERSION,
                    "type": "gateway.cache.event",
                    "sequence": 1,
                    "cache": {"outcome": "prefix_hint", "hintDepth": 2},
                },
            ),
            (
                "gateway.output_text.delta",
                {
                    "apiVersion": API_VERSION,
                    "type": "gateway.output_text.delta",
                    "sequence": 2,
                    "text": "fresh Provider output",
                },
            ),
            (
                "gateway.response.completed",
                _terminal(
                    3,
                    cache={"outcome": "prefix_hint", "hintDepth": 2},
                ),
            ),
        ]
        self.assertEqual(
            _sequence_violations(exact, self.oracle, self.sse_schema), []
        )
        self.assertEqual(
            _sequence_violations(prefix, self.oracle, self.sse_schema), []
        )

    def test_sequence_oracle_rejects_gaps_duplicate_terminals_and_post_terminal(
        self,
    ) -> None:
        malformed = [
            ("gateway.response.started", _started(0)),
            (
                "gateway.response.completed",
                _terminal(2),
            ),
            (
                "gateway.output_text.delta",
                {
                    "apiVersion": API_VERSION,
                    "type": "gateway.output_text.delta",
                    "sequence": 3,
                    "text": "too late",
                },
            ),
            ("gateway.response.uncertain", _terminal(4, status="uncertain")),
        ]
        failures = _sequence_violations(malformed, self.oracle, self.sse_schema)
        self.assertTrue(any("sequence must" in failure for failure in failures))
        self.assertTrue(any("after terminal" in failure for failure in failures))
        self.assertTrue(any("more than one terminal" in failure for failure in failures))

    def test_fallback_replay_is_forbidden_after_every_unsafe_boundary(self) -> None:
        fallback_event = {
            "apiVersion": API_VERSION,
            "type": "gateway.fallback.event",
            "sequence": 2,
            "fallback": {
                "attempted": True,
                "attemptCount": 2,
                "finalAction": "retry",
                "errorCode": "upstream_timeout",
                "retryable": True,
            },
        }
        delta = {
            "apiVersion": API_VERSION,
            "type": "gateway.output_text.delta",
            "sequence": 1,
            "text": "already public",
        }
        tool = {
            "apiVersion": API_VERSION,
            "type": "gateway.tool.observed",
            "sequence": 1,
            "tool": {"observed": True},
        }

        cases = {
            "output delta": ([('gateway.response.started', _started()), ('gateway.output_text.delta', delta), ('gateway.fallback.event', fallback_event)], {}),
            "tool": ([('gateway.response.started', _started()), ('gateway.tool.observed', tool), ('gateway.fallback.event', fallback_event)], {}),
            "Provider side effect": ([('gateway.response.started', _started()), ('gateway.cache.event', {"apiVersion": API_VERSION, "type": "gateway.cache.event", "sequence": 1, "cache": {"outcome": "miss"}}), ('gateway.fallback.event', fallback_event)], {"provider_side_effect_observed": True}),
            "client abort": ([('gateway.response.started', _started()), ('gateway.cache.event', {"apiVersion": API_VERSION, "type": "gateway.cache.event", "sequence": 1, "cache": {"outcome": "miss"}}), ('gateway.fallback.event', fallback_event)], {"client_aborted": True}),
        }
        for name, (events, kwargs) in cases.items():
            with self.subTest(case=name):
                failures = _sequence_violations(
                    events, self.oracle, self.sse_schema, **kwargs
                )
                self.assertTrue(any("unsafe fallback replay" in item for item in failures))


class GatewayProductionResponseContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.oracle = _SchemaOracle(cls.schema)
        cls.sse_schema = _event_schema(cls.schema)

    def test_runtime_success_matches_the_frozen_json_contract(self) -> None:
        def egress(_provider_id: str, _request_body: bytes, **_kwargs: object):
            return ProviderResult(
                200,
                (("Content-Type", "application/json"),),
                (
                    json.dumps(
                        {
                            "output_text": "contract output",
                            "usage": {"input_tokens": 3, "output_tokens": 4},
                        },
                        separators=(",", ":"),
                    ).encode("utf-8"),
                ),
            )

        response = GatewayRuntime(egress=egress)(
            {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "request": {
                    "model": "gpt-4.1-mini",
                    "input": "hello",
                    "stream": False,
                },
            }
        )
        response_schema = self.oracle.definition("gatewayResponse")
        self.assertTrue(
            self.oracle.accepts(response, response_schema),
            f"runtime response is outside the frozen contract: {response!r}",
        )

    def test_pre_runtime_rejections_keep_gateway_api_v1_problem_shape(self) -> None:
        gateway = GatewayRouter(
            GatewayMode.GATEWAY,
            policy=None,
            proxy=lambda _payload: _success(),
        )
        cases = {
            "unknown route": gateway.dispatch("POST", "/gateway/v1/missing", b"{}"),
            "wrong method": gateway.dispatch(
                "PUT", "/gateway/v1/responses", b"{}"
            ),
            "invalid JSON framing": gateway.dispatch(
                "POST", "/gateway/v1/responses", b"not-json"
            ),
            "body limit": gateway.dispatch(
                "POST", "/gateway/v1/responses", b"x" * (MAX_BODY_BYTES + 1)
            ),
            "observe mode": GatewayRouter(
                GatewayMode.OBSERVE, policy=None, proxy=None
            ).dispatch("POST", "/gateway/v1/responses", b"{}"),
            "proxy disabled": GatewayRouter(
                GatewayMode.GATEWAY, policy=None, proxy=None
            ).dispatch("POST", "/gateway/v1/responses", b"{}"),
        }
        expected_statuses = {
            "unknown route": 404,
            "wrong method": 405,
            "invalid JSON framing": 400,
            "body limit": 413,
            "observe mode": 404,
            "proxy disabled": 403,
        }
        for name, (status, payload) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(status, expected_statuses[name])
                self.assertEqual(set(payload), {"error"})
                self.assertEqual(
                    set(payload["error"]), {"code", "message", "retryable"}
                )
                self.assertNotIn("apiVersion", payload)
                self.assertNotIn("object", payload)
                self.assertFalse(self.oracle.accepts(payload))

    def test_accepted_runtime_error_uses_the_versioned_domain_contract(self) -> None:
        def egress(_provider_id: str, _request_body: bytes, **_kwargs: object):
            raise GatewayProviderError("upstream_timeout", True)

        response = GatewayRuntime(egress=egress)(
            {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "request": {
                    "model": "gpt-4.1-mini",
                    "input": "hello",
                    "stream": False,
                },
            }
        )
        self.assertTrue(
            self.oracle.accepts(response, self.oracle.definition("gatewayError")),
            response,
        )

    def test_router_rejects_forbidden_provider_material_at_public_boundary(
        self,
    ) -> None:
        secret = "sk-provider-secret-must-not-cross-boundary"
        poisoned = _success(
            providerHeaders={"authorization": secret},
            providerRequestId="req_private",
            prompt="private prompt",
            rawChunk=secret,
        )
        router = GatewayRouter(
            GatewayMode.GATEWAY,
            policy=None,
            proxy=lambda _payload: poisoned,
        )
        request_body = json.dumps(
            {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "request": {
                    "model": "gpt-4.1-mini",
                    "input": "hello",
                    "stream": False,
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        status, response = router.dispatch(
            "POST", "/gateway/v1/responses", request_body
        )

        self.assertEqual(status, 500)
        self.assertTrue(
            self.oracle.accepts(response, self.oracle.definition("gatewayError")),
            response,
        )
        serialized = json.dumps(response, sort_keys=True)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("providerRequestId", serialized)
        self.assertNotIn("prompt", serialized)
        self.assertNotIn("rawChunk", serialized)

    def test_json_and_sse_are_equivalent_and_report_effective_fallback_source(
        self,
    ) -> None:
        fallback = {
            "attempted": True,
            "attemptCount": 2,
            "finalAction": "retry",
            "errorCode": "upstream_timeout",
            "retryable": True,
        }
        expected = _success(
            provider="ollama",
            model="qwen",
            stream=True,
            outputText="fallback output",
            tool={"observed": False},
            cache={"outcome": "miss"},
            fallback=fallback,
        )
        router = GatewayRouter(
            GatewayMode.GATEWAY,
            policy=None,
            proxy=lambda _payload: expected,
        )
        body = json.dumps(
            {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "request": {"model": "gpt-4.1-mini", "stream": True},
            },
            separators=(",", ":"),
        ).encode("utf-8")

        json_status, json_response = router.dispatch(
            "POST", "/gateway/v1/responses", body
        )
        sse_status, raw = router.dispatch_sse(
            "POST", "/gateway/v1/responses", body
        )
        self.assertEqual((json_status, sse_status), (200, 200))
        events = _parse_sse(raw)
        failures = _sequence_violations(events, self.oracle, self.sse_schema)

        if events:
            started = events[0][1]
            if started.get("provider") != "openai" or started.get("model") != "gpt-4.1-mini":
                failures.append("started event must report accepted primary provider/model")
        terminals = [data for _, data in events if data.get("type") in TERMINAL_TYPES]
        if len(terminals) == 1:
            terminal = terminals[0]
            for field in ("provider", "model", "status", "usage", "tool", "cache", "fallback"):
                if terminal.get(field) != json_response.get(field):
                    failures.append(f"terminal {field} must equal bounded JSON {field}")
        else:
            failures.append("cannot compare JSON to a unique terminal SSE event")
        text = "".join(
            data["text"]
            for _, data in events
            if data.get("type") == "gateway.output_text.delta"
            and type(data.get("text")) is str
        )
        if text != json_response.get("outputText"):
            failures.append("SSE delta text must equal JSON outputText")

        self.assertEqual(failures, [])

    def test_router_exposes_incremental_dispatch_events(self) -> None:
        expected = _success(stream=True)
        router = GatewayRouter(
            GatewayMode.GATEWAY,
            policy=None,
            proxy=lambda _payload: expected,
        )
        dispatch_events = getattr(router, "dispatch_events", None)
        self.assertTrue(
            callable(dispatch_events),
            "GatewayRouter.dispatch_events must expose an incremental event iterator",
        )

        request_body = json.dumps(
            {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "request": {
                    "model": "gpt-4.1-mini",
                    "input": "hello",
                    "stream": True,
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        status, stream = dispatch_events(
            "POST", "/gateway/v1/responses", request_body
        )
        self.assertEqual(status, 200)
        self.assertNotIsInstance(stream, (bytes, bytearray, memoryview))
        normalized: list[tuple[str, dict[str, object]]] = []
        for event in stream:
            if type(event) is dict:
                normalized.append((event.get("type"), event))
            elif type(event) is tuple and len(event) == 2 and type(event[1]) is dict:
                normalized.append(event)
            else:
                self.fail(f"dispatch_events yielded an invalid event: {event!r}")
        self.assertEqual(
            _sequence_violations(normalized, self.oracle, self.sse_schema), []
        )

    def test_fallback_policy_cannot_replay_after_output_becomes_public(self) -> None:
        try:
            output_visible = ReplayState(output_delta_observed=True)
        except TypeError as error:
            self.fail(
                "ReplayState must represent an already-public output delta: "
                f"{error}"
            )

        selector = FallbackSelector(
            cost_cap_multiplier=3.0,
            on_exhausted=FallbackAction.QUEUE,
        )
        candidate = Candidate("ollama", "qwen", 0.0, Decision.YES)
        states = {
            "output delta": output_visible,
            "tool": ReplayState(tool_call_observed=True),
            "side effect": ReplayState(provider_side_effect_observed=True),
            "client abort": ReplayState(client_aborted=True),
        }
        for name, state in states.items():
            with self.subTest(case=name):
                decision = selector.decide(
                    failure=FailureSignal.TIMEOUT,
                    retries_used=0,
                    primary_cost=1.0,
                    candidates=(candidate,),
                    replay_state=state,
                )
                self.assertEqual(decision.action, FallbackAction.FAIL)
                self.assertIsNone(decision.candidate)
                self.assertFalse(decision.retryable)


if __name__ == "__main__":
    unittest.main()
