import json
import unittest
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "runtime-observation-v1.json"


def valid_observation() -> dict[str, object]:
    return {
        "observationId": "obs_0123456789abcdef0123456789abcdef",
        "providerId": "openai",
        "modelId": "gpt-5",
        "scopeRef": "anon_0123456789abcdef",
        "startedAt": "2026-08-01T00:00:00Z",
        "firstTokenAt": "2026-08-01T00:00:01Z",
        "completedAt": "2026-08-01T00:00:03Z",
        "inputTokens": 12,
        "outputTokens": 3,
        "cacheReadTokens": 4,
        "cacheCreationTokens": 0,
        "reasoningTokens": 1,
        "totalTokens": 20,
        "tokenCountingConvention": "components_disjoint",
        "status": "completed",
        "costMicros": 25,
        "costCurrency": "usd",
        "sourceId": "litellm.otel",
        "quality": "provider_reported",
    }


def document(observations: list[dict[str, object]] | None = None) -> str:
    return json.dumps({
        "schemaVersion": 1,
        "observations": observations if observations is not None else [valid_observation()],
    })


class RuntimeObservationContractTests(unittest.TestCase):
    def test_frozen_v1_fixture_matches_the_strict_decoder(self):
        from openusage_bar.runtime_observation import decode_runtime_document

        decoded = decode_runtime_document(FIXTURE.read_bytes())
        self.assertEqual(decoded.schema_version, 1)
        self.assertEqual(decoded.observations[0].source_id, "litellm.otel")

    def test_decodes_strict_terminal_observation_and_derives_latency(self):
        from openusage_bar.runtime_observation import decode_runtime_document

        decoded = decode_runtime_document(document())

        self.assertEqual(decoded.schema_version, 1)
        self.assertEqual(len(decoded.observations), 1)
        observation = decoded.observations[0]
        self.assertEqual(observation.provider_id, "openai")
        self.assertEqual(observation.model_id, "gpt-5")
        self.assertEqual(observation.scope_ref, "anon_0123456789abcdef")
        self.assertEqual(observation.ttft_ms, 1000)
        self.assertEqual(observation.duration_ms, 3000)
        self.assertEqual(observation.total_tokens, 20)

    def test_rejects_unknown_or_private_fields_at_every_boundary(self):
        from openusage_bar.runtime_observation import (
            RuntimeObservationDecodeError,
            decode_runtime_document,
        )

        for key in (
            "prompt", "response", "apiKey", "credential", "cookie",
            "requestId", "accountRef", "rawPayload", "email",
        ):
            with self.subTest(key=key):
                observation = valid_observation()
                observation[key] = "private"
                with self.assertRaises(RuntimeObservationDecodeError):
                    decode_runtime_document(document([observation]))
        payload = json.loads(document())
        payload["future"] = True
        with self.assertRaises(RuntimeObservationDecodeError):
            decode_runtime_document(json.dumps(payload))

    def test_requires_canonical_ids_and_anonymous_scope(self):
        from openusage_bar.runtime_observation import (
            RuntimeObservationDecodeError,
            decode_runtime_document,
        )

        for key, value in (
            ("observationId", "upstream-request-123"),
            ("providerId", "openai/account"),
            ("modelId", "gpt 5"),
            ("scopeRef", "customer@example.com"),
            ("scopeRef", "account-123"),
            ("sourceId", "litellm bearer secret"),
            ("quality", "trusted"),
        ):
            with self.subTest(key=key, value=value):
                observation = valid_observation()
                observation[key] = value
                with self.assertRaises(RuntimeObservationDecodeError):
                    decode_runtime_document(document([observation]))

    def test_requires_utc_ordered_timestamps_and_bounded_duration(self):
        from openusage_bar.runtime_observation import (
            RuntimeObservationDecodeError,
            decode_runtime_document,
        )

        cases = (
            ("startedAt", "2026-08-01T00:00:00+01:00"),
            ("firstTokenAt", "2026-07-31T23:59:59Z"),
            ("completedAt", "2026-07-31T23:59:59Z"),
            ("completedAt", "2026-08-02T00:00:00.001Z"),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                observation = valid_observation()
                observation[key] = value
                with self.assertRaises(RuntimeObservationDecodeError):
                    decode_runtime_document(document([observation]))

    def test_requires_first_token_when_output_was_observed(self):
        from openusage_bar.runtime_observation import (
            RuntimeObservationDecodeError,
            decode_runtime_document,
        )

        observation = valid_observation()
        observation["firstTokenAt"] = None
        with self.assertRaises(RuntimeObservationDecodeError):
            decode_runtime_document(document([observation]))

        observation["outputTokens"] = 0
        observation["totalTokens"] = 17
        decoded = decode_runtime_document(document([observation]))
        self.assertIsNone(decoded.observations[0].ttft_ms)

    def test_enforces_token_conventions_and_integer_bounds(self):
        from openusage_bar.runtime_observation import (
            RuntimeObservationDecodeError,
            decode_runtime_document,
        )

        invalid = []
        mismatch = valid_observation()
        mismatch["totalTokens"] = 19
        invalid.append(mismatch)
        cache_over_input = valid_observation()
        cache_over_input["tokenCountingConvention"] = "input_includes_cache"
        cache_over_input["inputTokens"] = 3
        cache_over_input["totalTokens"] = 6
        invalid.append(cache_over_input)
        boolean = valid_observation()
        boolean["inputTokens"] = True
        invalid.append(boolean)
        oversized = valid_observation()
        oversized["outputTokens"] = 1_000_000_000_001
        invalid.append(oversized)
        bad_convention = valid_observation()
        bad_convention["tokenCountingConvention"] = "guess"
        invalid.append(bad_convention)

        for observation in invalid:
            with self.subTest(observation=observation):
                with self.assertRaises(RuntimeObservationDecodeError):
                    decode_runtime_document(document([observation]))

    def test_requires_terminal_status_and_consistent_cost_pair(self):
        from openusage_bar.runtime_observation import (
            RuntimeObservationDecodeError,
            decode_runtime_document,
        )

        for key, value in (
            ("status", "streaming"),
            ("costMicros", -1),
            ("costMicros", 1_000_000_000_001),
            ("costCurrency", "USD"),
        ):
            with self.subTest(key=key, value=value):
                observation = valid_observation()
                observation[key] = value
                with self.assertRaises(RuntimeObservationDecodeError):
                    decode_runtime_document(document([observation]))
        observation = valid_observation()
        observation["costMicros"] = None
        with self.assertRaises(RuntimeObservationDecodeError):
            decode_runtime_document(document([observation]))

    def test_rejects_duplicate_oversized_or_excessive_batches(self):
        from openusage_bar.runtime_observation import (
            MAX_DOCUMENT_BYTES,
            RuntimeObservationDecodeError,
            decode_runtime_document,
        )

        duplicate = [valid_observation(), valid_observation()]
        with self.assertRaises(RuntimeObservationDecodeError):
            decode_runtime_document(document(duplicate))

        observations = []
        for index in range(257):
            observation = valid_observation()
            observation["observationId"] = f"obs_{index:032x}"
            observations.append(observation)
        with self.assertRaises(RuntimeObservationDecodeError):
            decode_runtime_document(document(observations))

        oversized = b"{" + b" " * MAX_DOCUMENT_BYTES + b"}"
        with self.assertRaises(RuntimeObservationDecodeError):
            decode_runtime_document(oversized)

    def test_complete_empty_document_is_a_valid_covered_batch(self):
        from openusage_bar.runtime_observation import decode_runtime_document

        decoded = decode_runtime_document(document([]))
        self.assertEqual(decoded.observations, ())
