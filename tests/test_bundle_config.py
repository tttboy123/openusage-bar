import plistlib
import unittest
from pathlib import Path

from build_support import patch_static_zlib_source
from openusage_bar.bundle_config import (
    APP_BUNDLE_PATH, APP_VERSION, BUILD_VERSION, BUNDLE_ID, info_plist, launch_agent_payload,
)


class BundleConfigTests(unittest.TestCase):
    def test_static_zlib_patch_guards_missing_module_file(self):
        source = "            self.copy_file(zlib.__file__, os.path.dirname(arcdir))\n"

        patched = patch_static_zlib_source(source)

        self.assertIn('if getattr(zlib, "__file__", None):', patched)
        self.assertIn("self.copy_file(zlib.__file__, os.path.dirname(arcdir))", patched)

    def test_build_definition_uses_bundle_config_and_py2app(self):
        source = Path("setup.py").read_text(encoding="utf-8")

        self.assertIn("from openusage_bar.bundle_config import APP_NAME, APP_VERSION, info_plist", source)
        self.assertIn("version=APP_VERSION", source)
        self.assertIn('app=["openusage_settings.py"]', source)
        self.assertIn('"packages": ["openusage_bar"]', source)
        self.assertIn('"resources/*.json"', source)
        self.assertEqual(
            list(Path("openusage_bar/resources").glob("provider-catalog.*.json")),
            [Path("openusage_bar/resources/provider-catalog.v1.json")],
        )

    def test_info_plist_defines_stable_agent_application(self):
        plist = info_plist()

        self.assertEqual(BUNDLE_ID, "com.lune.openusagebar.settings")
        self.assertEqual(plist["CFBundleIdentifier"], BUNDLE_ID)
        self.assertEqual(plist["CFBundleDisplayName"], "OpenUsage Provider Settings")
        self.assertNotIn("LSUIElement", plist)
        self.assertEqual(plist["LSMinimumSystemVersion"], "15.0")

    def test_all_three_bundles_share_the_canonical_version(self):
        expected = (APP_VERSION, BUILD_VERSION)
        self.assertEqual(
            (info_plist()["CFBundleShortVersionString"], info_plist()["CFBundleVersion"]),
            expected,
        )
        resources = Path("swift_app/Resources")
        for name in ("OpenUsageBar-Info.plist", "OpenUsageActivity-Info.plist", "OpenUsageProviderSettings-Info.plist"):
            with (resources / name).open("rb") as handle:
                payload = plistlib.load(handle)
            self.assertEqual((payload["CFBundleShortVersionString"], payload["CFBundleVersion"]), expected)

    def test_launch_agent_runs_the_bundle_executable(self):
        payload = launch_agent_payload("/tmp/stdout.log", "/tmp/stderr.log")

        self.assertEqual(
            payload["ProgramArguments"],
            [
                f"{APP_BUNDLE_PATH}/Contents/Helpers/OpenUsage Provider Settings.app/Contents/MacOS/OpenUsage Provider Settings",
                "daemon", "--interval", "300", "--api-socket",
                "~/.local/state/openusage-bar/openusage.sock",
            ],
        )
        self.assertTrue(payload["KeepAlive"])
        self.assertTrue(payload["RunAtLoad"])


if __name__ == "__main__":
    unittest.main()
