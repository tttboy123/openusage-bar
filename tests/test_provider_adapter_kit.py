from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from openusage_bar.config import (
    DailyCostFeedConfig,
    DailyUsageFeedConfig,
    GenericProviderConfig,
)
from scripts.check_provider_adapter import validate_bundle
from tests.provider_conformance import (
    REQUIRED_CASES,
    load_provider_adapter_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "provider-adapter-kit" / "example-provider"


class ProviderAdapterKitTests(unittest.TestCase):
    def test_example_bundle_covers_every_case_and_declarative_feed_kind(self):
        bundle = load_provider_adapter_bundle(EXAMPLE)

        self.assertEqual(bundle.fixture.cases, REQUIRED_CASES)
        self.assertEqual(
            tuple(type(config) for config in bundle.configs),
            (GenericProviderConfig, DailyUsageFeedConfig, DailyCostFeedConfig),
        )
        self.assertEqual(
            [config.provider_id for config in bundle.configs],
            ["example-quota", "example-usage", "example-cost"],
        )
        self.assertTrue(all(config.account_ref == "fixture-a" for config in bundle.configs))
        self.assertIsNone(bundle.fixture.unknown_value)

    def test_public_checker_returns_only_bounded_non_identity_summary(self):
        result = validate_bundle(EXAMPLE)
        self.assertEqual(result, {
            "bundle": "example-provider",
            "caseCount": len(REQUIRED_CASES),
            "configKinds": ["generic", "daily_usage_feed", "daily_cost_feed"],
            "ok": True,
        })
        completed = subprocess.run(
            [sys.executable, "scripts/check_provider_adapter.py", str(EXAMPLE)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), result)
        self.assertNotIn(str(ROOT), completed.stdout + completed.stderr)

    def test_checker_rejects_missing_cases_secret_material_and_wrong_templates(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "example-provider"
            bundle.mkdir()
            for source in EXAMPLE.iterdir():
                if source.is_file():
                    (bundle / source.name).write_bytes(source.read_bytes())
                else:
                    target = bundle / source.name
                    target.mkdir()
                    for child in source.iterdir():
                        (target / child.name).write_bytes(child.read_bytes())

            fixture_path = bundle / "fixtures" / "conformance.json"
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            fixture["cases"].pop("unknown_not_zero")
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "conformance matrix"):
                validate_bundle(bundle)

            fixture = json.loads(
                (EXAMPLE / "fixtures" / "conformance.json").read_text(
                    encoding="utf-8"
                )
            )
            fixture["note"] = "sk-example_private_credential"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "credential or identity"):
                validate_bundle(bundle)

            fixture_path.write_bytes(
                (EXAMPLE / "fixtures" / "conformance.json").read_bytes()
            )
            (bundle / "daily-cost.providers.json").unlink()
            with self.assertRaisesRegex(ValueError, "three declarative templates"):
                validate_bundle(bundle)

            manifest_path = bundle / "manifest.json"
            manifest = json.loads(
                (EXAMPLE / "manifest.json").read_text(encoding="utf-8")
            )
            manifest["fixture_id"] = "not a public slug"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "identity is invalid"):
                validate_bundle(bundle)

    def test_cli_sanitizes_unexpected_invalid_bundle_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "example-provider"
            bundle.mkdir()
            manifest = json.loads(
                (EXAMPLE / "manifest.json").read_text(encoding="utf-8")
            )
            manifest["cases"] = [["not", "hashable"]]
            (bundle / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            completed = subprocess.run(
                [sys.executable, "scripts/check_provider_adapter.py", str(bundle)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(
            json.loads(completed.stderr),
            {"error": "invalid_bundle", "ok": False},
        )
        self.assertNotIn(str(bundle), completed.stderr)

    def test_docs_publish_one_safe_copyable_command_and_security_boundaries(self):
        guide = (ROOT / "docs" / "provider-adapter-kit.md").read_text(
            encoding="utf-8"
        )
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

        self.assertIn(
            "python3 scripts/check_provider_adapter.py "
            "examples/provider-adapter-kit/example-provider",
            guide,
        )
        for required in (
            "Keychain",
            "HTTPS",
            "redirect",
            "pagination",
            "response size",
            "sanitized errors",
            "Last-good",
            "Unknown",
        ):
            self.assertIn(required, guide)
        self.assertIn("Provider Adapter Kit", contributing)

    def test_provider_request_template_never_solicits_credentials(self):
        template = (
            ROOT / ".github" / "ISSUE_TEMPLATE" / "provider_request.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("Provider Adapter Kit", template)
        self.assertIn("Do not paste", template)
        self.assertNotIn("placeholder: sk-", template)
        self.assertNotIn("placeholder: Bearer", template)


if __name__ == "__main__":
    unittest.main()
