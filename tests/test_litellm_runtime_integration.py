import importlib.util
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "integrations" / "litellm_openusage.py"
FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "runtime-producers"
    / "litellm-success-v1.json"
)
SCOPE_REF = "anon_0123456789abcdef"


def load_integration():
    spec = importlib.util.spec_from_file_location(
        "openusage_litellm_integration", INTEGRATION
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("integration module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def fixture_times(payload: dict[str, object]) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(str(payload["startTime"]).replace("Z", "+00:00"))
    end = datetime.fromisoformat(str(payload["endTime"]).replace("Z", "+00:00"))
    return start, end


class AttributeValue:
    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


def as_attributes(value: object) -> object:
    if isinstance(value, dict):
        return AttributeValue(**{key: as_attributes(child) for key, child in value.items()})
    if isinstance(value, list):
        return [as_attributes(child) for child in value]
    return value


class LiteLLMRuntimeTransformTests(unittest.TestCase):
    def test_content_bearing_success_becomes_one_strict_private_observation(self):
        from openusage_bar.runtime_observation import decode_runtime_document

        integration = load_integration()
        fixture = load_fixture()
        start, end = fixture_times(fixture)

        result = integration.build_runtime_document(
            fixture["kwargs"],
            fixture["response"],
            start,
            end,
            scope_ref=SCOPE_REF,
            provider_map={"openai": "openai"},
            status="completed",
        )

        self.assertIsNotNone(result)
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        decoded = decode_runtime_document(encoded)
        self.assertEqual(len(decoded.observations), 1)
        row = decoded.observations[0]
        self.assertRegex(row.observation_id, r"^obs_[0-9a-f]{32}$")
        self.assertEqual(row.provider_id, "openai")
        self.assertEqual(row.model_id, "openai.gpt-5")
        self.assertEqual(row.scope_ref, SCOPE_REF)
        self.assertEqual(row.input_tokens, 12)
        self.assertEqual(row.output_tokens, 3)
        self.assertEqual(row.cache_read_tokens, 4)
        self.assertEqual(row.cache_creation_tokens, 0)
        self.assertEqual(row.reasoning_tokens, 1)
        self.assertEqual(row.total_tokens, 15)
        self.assertEqual(row.token_counting_convention, "input_includes_cache")
        self.assertIsNone(row.first_token_at)
        self.assertEqual(row.cost_micros, 25)
        self.assertEqual(row.cost_currency, "usd")
        self.assertEqual(row.source_id, "litellm.callback.v1")
        self.assertEqual(row.quality, "estimated")
        for private in (
            "private fixture prompt",
            "private fixture response",
            "fixture-redacted-value",
            "private failure text",
            "upstream-call-id-not-for-storage",
            "upstream-response-id-not-for-storage",
        ):
            with self.subTest(private=private):
                self.assertNotIn(private, encoded)

    def test_attribute_style_usage_and_known_first_token_time_are_supported(self):
        from openusage_bar.runtime_observation import decode_runtime_document

        integration = load_integration()
        fixture = load_fixture()
        start, end = fixture_times(fixture)
        kwargs = dict(fixture["kwargs"])
        kwargs["completion_start_time"] = start + timedelta(milliseconds=750)

        result = integration.build_runtime_document(
            kwargs,
            as_attributes(fixture["response"]),
            start,
            end,
            scope_ref=SCOPE_REF,
            provider_map={"openai": "openai"},
            status="completed",
        )

        self.assertIsNotNone(result)
        decoded = decode_runtime_document(json.dumps(result))
        self.assertEqual(decoded.observations[0].ttft_ms, 750)

    def test_missing_or_inconsistent_usage_never_becomes_zero(self):
        integration = load_integration()
        fixture = load_fixture()
        start, end = fixture_times(fixture)
        response = dict(fixture["response"])

        for usage in (
            None,
            {},
            {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 16},
            {"prompt_tokens": True, "completion_tokens": 3, "total_tokens": 4},
        ):
            with self.subTest(usage=usage):
                candidate = dict(response)
                candidate["usage"] = usage
                self.assertIsNone(integration.build_runtime_document(
                    fixture["kwargs"],
                    candidate,
                    start,
                    end,
                    scope_ref=SCOPE_REF,
                    provider_map={"openai": "openai"},
                    status="completed",
                ))

    def test_unsafe_identity_model_scope_or_time_fails_closed(self):
        integration = load_integration()
        fixture = load_fixture()
        start, end = fixture_times(fixture)
        cases = (
            ({"openai": "openai/account"}, SCOPE_REF, start, end),
            ({}, SCOPE_REF, start, end),
            ({"openai": "openai"}, "account@example.com", start, end),
            ({"openai": "openai"}, SCOPE_REF, start.replace(tzinfo=None), end),
            ({"openai": "openai"}, SCOPE_REF, end, start),
        )

        for provider_map, scope_ref, selected_start, selected_end in cases:
            with self.subTest(
                provider_map=provider_map,
                scope_ref=scope_ref,
                start=selected_start,
                end=selected_end,
            ):
                self.assertIsNone(integration.build_runtime_document(
                    fixture["kwargs"],
                    fixture["response"],
                    selected_start,
                    selected_end,
                    scope_ref=scope_ref,
                    provider_map=provider_map,
                    status="completed",
                ))

        unsafe_model = dict(fixture["kwargs"])
        unsafe_model["model"] = "openai/model with spaces"
        self.assertIsNone(integration.build_runtime_document(
            unsafe_model,
            fixture["response"],
            start,
            end,
            scope_ref=SCOPE_REF,
            provider_map={"openai": "openai"},
            status="completed",
        ))

    def test_scope_generator_returns_unlinked_canonical_values(self):
        integration = load_integration()

        first = integration.create_scope_ref()
        second = integration.create_scope_ref()

        self.assertRegex(first, r"^anon_[0-9a-f]{32}$")
        self.assertRegex(second, r"^anon_[0-9a-f]{32}$")
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
