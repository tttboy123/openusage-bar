from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from openusage_bar.routing_commands import run_routing_mutation
from openusage_bar.routing_targets import RouteTargetStore


def target_payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "targetId": "openai.work.gpt-5",
        "providerId": "openai",
        "accountRef": "account-1",
        "modelId": "gpt-5",
        "connectionRef": "connection-1",
        "executionClass": "direct_api",
        "executionAdapterId": "openai.direct",
        "resourceMode": "quota",
        "factAccountRef": "account-1",
        "runtimeScopeRef": None,
        "balanceCurrency": None,
        "costCurrency": "USD",
        "inputCostMicrosPerMillion": 3_000_000,
        "outputCostMicrosPerMillion": 3_000_000,
        "enabled": True,
        "regions": ["global"],
        "privacyClass": "direct_provider",
        "capabilities": ["chat", "reasoning", "tools"],
        "contextWindowTokens": 400_000,
        "qualityTier": 4,
    }
    value.update(changes)
    return value


def request(*, expected_revision: int, targets: list[dict[str, object]]) -> str:
    return json.dumps({
        "version": 1,
        "action": "replace_targets",
        "expectedRevision": expected_revision,
        "targets": targets,
    })


class RoutingMutationCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "route-targets.json"
        self.store = RouteTargetStore(self.path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def mutate(self, raw: str) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        status = run_routing_mutation(io.StringIO(raw), output, store=self.store)
        return status, json.loads(output.getvalue())

    def test_replaces_targets_with_monotonic_revision_and_private_file(self) -> None:
        status, result = self.mutate(request(expected_revision=0, targets=[target_payload()]))
        self.assertEqual(status, 0)
        self.assertEqual(result, {
            "version": 1,
            "ok": True,
            "message": "Routing targets saved",
            "targetRevision": 1,
        })
        configuration = self.store.load(available_adapters=("openai.direct",))
        self.assertEqual(configuration.revision, 1)
        self.assertEqual(configuration.targets[0].target_id, "openai.work.gpt-5")
        self.assertTrue(configuration.targets[0].adapter_available)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_stale_revision_preserves_the_previous_document(self) -> None:
        self.mutate(request(expected_revision=0, targets=[target_payload()]))
        before = self.path.read_bytes()
        status, result = self.mutate(request(
            expected_revision=0,
            targets=[target_payload(modelId="gpt-5-mini")],
        ))
        self.assertEqual(status, 1)
        self.assertEqual(result, {
            "version": 1,
            "ok": False,
            "message": "Routing targets changed; reload before saving",
        })
        self.assertEqual(self.path.read_bytes(), before)

    def test_save_failure_returns_sanitized_error_and_preserves_document(self) -> None:
        self.mutate(request(expected_revision=0, targets=[target_payload()]))
        before = self.path.read_bytes()

        class FailingStore:
            def load(inner_self, *, available_adapters: object):
                return self.store.load(available_adapters=available_adapters)  # type: ignore[arg-type]

            def save(inner_self, targets: object, *, revision: int) -> None:
                raise OSError("private filesystem detail")

        output = io.StringIO()
        status = run_routing_mutation(
            io.StringIO(request(expected_revision=1, targets=[])),
            output,
            store=FailingStore(),  # type: ignore[arg-type]
        )
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue()), {
            "version": 1,
            "ok": False,
            "message": "Routing targets could not be saved",
        })
        self.assertEqual(self.path.read_bytes(), before)

    def test_rejects_credentials_unknown_fields_duplicates_and_invalid_targets(self) -> None:
        hostile = json.loads(request(expected_revision=0, targets=[target_payload()]))
        hostile["apiKey"] = "secret-sentinel"
        cases = [
            json.dumps(hostile),
            '{"version":1,"version":1,"action":"replace_targets","expectedRevision":0,"targets":[]}',
            request(expected_revision=0, targets=[target_payload(accountRef="person@example.com")]),
            request(expected_revision=0, targets=[target_payload(capabilities=["chat", "chat"])]),
            request(expected_revision=0, targets=[target_payload(enabled=1)]),
        ]
        for raw in cases:
            with self.subTest(raw=raw[:80]):
                status, result = self.mutate(raw)
                self.assertEqual(status, 1)
                self.assertEqual(result, {
                    "version": 1,
                    "ok": False,
                    "message": "Routing target request is invalid",
                })
                self.assertFalse(self.path.exists())
                self.assertNotIn("secret", json.dumps(result).lower())

    def test_rejects_oversized_input_without_touching_storage(self) -> None:
        status, result = self.mutate(" " * (262_144 + 1))
        self.assertEqual(status, 1)
        self.assertFalse(result["ok"])
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
