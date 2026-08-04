import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts/generate_swift_provider_catalog.py"


def load_generator():
    spec = importlib.util.spec_from_file_location("swift_provider_catalog_generator", GENERATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError("generator module unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SwiftProviderCatalogGeneratorTests(unittest.TestCase):
    def test_render_emits_sorted_upstream_family_ids_from_manifest(self):
        rendered = load_generator().render()
        self.assertIn("public static let upstreamFamilyIDs: Set<String> = [", rendered)
        self.assertIn('        "alibaba_cloud",', rendered)
        self.assertIn('        "zed",', rendered)
        self.assertNotIn('        "minimax",\n        "step_plan",\n    ]\n    public static let families', rendered)

    def test_render_emits_source_evidence_metadata(self):
        rendered = load_generator().render()
        self.assertIn(
            "factFamilies: [.detection, .subscriptionCapacity, .tokenActivity], "
            "authority: .providerLocal, accountScope: .localProfile, "
            "modelScope: .mixed, verification: .liveAccount",
            rendered,
        )

    def test_render_emits_quick_connect_console_urls(self):
        rendered = load_generator().render()

        self.assertIn(
            "public static let quickConnectURLs: [String: String] = [",
            rendered,
        )
        self.assertIn('"deepseek": "https://platform.deepseek.com",', rendered)
        self.assertIn('"codex": "https://chatgpt.com/codex",', rendered)
        self.assertIn(
            "factFamilies: [.detection, .tokenActivity], "
            "authority: .thirdParty, accountScope: .localProfile, "
            "modelScope: .perModel, verification: .fixture",
            rendered,
        )

    def test_non_detection_sources_cannot_become_provider_identity(self):
        rendered = load_generator().render()
        minimax = rendered.split(
            '"minimax": ProviderDisplayDescriptor(', 1
        )[1].split(
            '"mistral": ProviderDisplayDescriptor(', 1
        )[0]

        self.assertIn(
            'ProviderIdentitySource(credentialSource: "minimax_builtin_api"',
            minimax,
        )
        self.assertNotIn(
            'ProviderIdentitySource(credentialSource: '
            '"minimax_china_billing_web"',
            minimax,
        )
        self.assertIn(
            'ProviderSourceCapability(sourceID: '
            '"minimax_china_billing_web"',
            minimax,
        )

    def test_swift_string_escapes_literals_interpolation_and_control_scalars(self):
        swift_string = load_generator().swift_string
        cases = {
            'quote"': '"quote\\\""',
            "backslash\\": '"backslash\\\\"',
            r"\(secret)": '"\\\\(secret)"',
            "line\nfeed": '"line\\nfeed"',
            "horizontal\ttab": '"horizontal\\ttab"',
            "nul\0byte": '"nul\\0byte"',
            "carriage\rreturn": '"carriage\\rreturn"',
            "bell\x07": '"bell\\u{7}"',
            "delete\x7f": '"delete\\u{7f}"',
            "c1\x85": '"c1\\u{85}"',
        }
        for value, expected in cases.items():
            with self.subTest(value=repr(value)):
                self.assertEqual(swift_string(value), expected)


if __name__ == "__main__":
    unittest.main()
