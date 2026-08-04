import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PublicAssetTests(unittest.TestCase):
    def test_readmes_reference_existing_public_images(self):
        expected = {
            "docs/assets/brand/openusage-bar-icon.png",
            "docs/assets/openusage-bar-activity-demo-zh.png",
            "docs/assets/openusage-bar-menu-demo.png",
            "docs/assets/openusage-bar-provider-catalog-demo-zh.png",
        }
        for readme_name in ("README.md", "README.en.md"):
            readme = (ROOT / readme_name).read_text(encoding="utf-8")
            for relative in expected:
                self.assertIn(relative, readme)
                payload = (ROOT / relative).read_bytes()
                self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"), relative)

    def test_checked_in_bundle_icon_is_nonempty_icns(self):
        icon = ROOT / "swift_app/Resources/OpenUsageBar.icns"
        payload = icon.read_bytes()

        self.assertGreater(len(payload), 100_000)
        self.assertEqual(payload[:4], b"icns")

    def test_icon_regeneration_script_is_executable_and_relocatable(self):
        script = ROOT / "scripts/generate_app_icon.sh"
        source = script.read_text(encoding="utf-8")

        self.assertTrue(os.access(script, os.X_OK))
        self.assertIn("ROOT=${0:A:h:h}", source)
        self.assertNotIn("/Users/", source)


if __name__ == "__main__":
    unittest.main()
