import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_routing_engine import target


class RouteTargetStoreTests(unittest.TestCase):
    def test_missing_configuration_is_empty_and_does_not_discover_targets(self):
        from openusage_bar.routing_targets import RouteTargetStore

        with tempfile.TemporaryDirectory() as directory:
            store = RouteTargetStore(Path(directory) / "routing-targets.json")

            configuration = store.load(available_adapters={"openai.direct"})

            self.assertEqual(configuration.schema_version, 1)
            self.assertEqual(configuration.revision, 0)
            self.assertEqual(configuration.targets, ())

    def test_round_trip_is_canonical_private_and_adapter_aware(self):
        from openusage_bar.routing_targets import RouteTargetStore

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "routing-targets.json"
            store = RouteTargetStore(path)
            original = target("openai.work.gpt-5")

            store.save((original,), revision=7)
            unavailable = store.load(available_adapters=set())
            available = store.load(available_adapters={"openai.direct"})

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(unavailable.revision, 7)
            self.assertFalse(unavailable.targets[0].adapter_available)
            self.assertTrue(available.targets[0].adapter_available)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schemaVersion"], 1)
            self.assertEqual(payload["targets"][0]["executionAdapterId"], "openai.direct")
            self.assertEqual(payload["targets"][0]["resourceMode"], "quota")
            self.assertEqual(payload["targets"][0]["factAccountRef"], "account-1")
            self.assertEqual(
                payload["targets"][0]["runtimeScopeRef"],
                "anon_0123456789abcdef",
            )
            self.assertNotIn("adapterAvailable", payload["targets"][0])
            self.assertNotIn("secret", path.read_text(encoding="utf-8").lower())

    def test_rejects_unknown_duplicate_trailing_or_private_fields(self):
        from openusage_bar.routing_targets import (
            RouteTargetConfigError,
            RouteTargetStore,
        )

        base = {
            "schemaVersion": 1,
            "revision": 1,
            "targets": [{
                "targetId": "openai.work.gpt-5",
                "providerId": "openai",
                "accountRef": "account-1",
                "modelId": "gpt-5",
                "connectionRef": "connection-1",
                "executionClass": "direct_api",
                "executionAdapterId": "openai.direct",
                "resourceMode": "quota",
                "runtimeScopeRef": "anon_0123456789abcdef",
                "factAccountRef": "account-1",
                "enabled": True,
                "regions": ["global"],
                "privacyClass": "direct_provider",
                "capabilities": ["chat", "reasoning", "tools"],
                "contextWindowTokens": 400000,
                "qualityTier": 4,
            }],
        }
        documents = []
        unknown = json.loads(json.dumps(base))
        unknown["future"] = True
        documents.append(json.dumps(unknown))
        private = json.loads(json.dumps(base))
        private["targets"][0]["apiKey"] = "sk-private"
        documents.append(json.dumps(private))
        documents.append('{"schemaVersion":1,"schemaVersion":1,"revision":1,"targets":[]}')
        documents.append(json.dumps(base) + " trailing")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routing-targets.json"
            store = RouteTargetStore(path)
            for document in documents:
                with self.subTest(document=document[-24:]):
                    path.write_text(document, encoding="utf-8")
                    os.chmod(path, 0o600)
                    with self.assertRaisesRegex(RouteTargetConfigError, "invalid route target configuration"):
                        store.load(available_adapters={"openai.direct"})

    def test_rejects_duplicate_ids_excess_targets_and_boolean_revision(self):
        from openusage_bar.routing_targets import RouteTargetConfigError, RouteTargetStore

        with tempfile.TemporaryDirectory() as directory:
            store = RouteTargetStore(Path(directory) / "routing-targets.json")
            with self.assertRaises(RouteTargetConfigError):
                store.save((target("same"), target("same")), revision=1)
            with self.assertRaises(RouteTargetConfigError):
                store.save(tuple(target(f"target-{index}") for index in range(129)), revision=1)
            with self.assertRaises(RouteTargetConfigError):
                store.save((), revision=True)

    def test_rejects_symlink_or_non_private_configuration(self):
        from openusage_bar.routing_targets import RouteTargetConfigError, RouteTargetStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real.json"
            real.write_text('{"schemaVersion":1,"revision":0,"targets":[]}', encoding="utf-8")
            os.chmod(real, 0o600)
            link = root / "routing-targets.json"
            link.symlink_to(real)
            with self.assertRaises(RouteTargetConfigError):
                RouteTargetStore(link).load(available_adapters=set())

            link.unlink()
            link.write_text('{"schemaVersion":1,"revision":0,"targets":[]}', encoding="utf-8")
            os.chmod(link, 0o644)
            with self.assertRaises(RouteTargetConfigError):
                RouteTargetStore(link).load(available_adapters=set())

    def test_failed_save_preserves_last_good_document(self):
        from openusage_bar.routing_targets import RouteTargetConfigError, RouteTargetStore

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routing-targets.json"
            store = RouteTargetStore(path)
            store.save((target("good"),), revision=1)
            before = path.read_bytes()

            with self.assertRaises(RouteTargetConfigError):
                store.save((target("duplicate"), target("duplicate")), revision=2)

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(store.load(available_adapters={"openai.direct"}).targets[0].target_id, "good")

    def test_read_handles_short_os_reads_and_revision_must_advance(self):
        from openusage_bar.routing_targets import RouteTargetConfigError, RouteTargetStore

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routing-targets.json"
            store = RouteTargetStore(path)
            store.save((target("good"),), revision=2)
            original_read = os.read
            with mock.patch(
                "openusage_bar.routing_targets.os.read",
                side_effect=lambda descriptor, size: original_read(descriptor, min(size, 7)),
            ):
                loaded = store.load(available_adapters={"openai.direct"})
            self.assertEqual(loaded.targets[0].target_id, "good")

            with self.assertRaises(RouteTargetConfigError):
                store.save((target("replacement"),), revision=2)
            with self.assertRaises(RouteTargetConfigError):
                store.save((target("replacement"),), revision=1)


if __name__ == "__main__":
    unittest.main()
