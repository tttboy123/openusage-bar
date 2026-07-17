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
