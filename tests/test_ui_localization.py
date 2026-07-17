import unittest
from pathlib import Path

from openusage_bar.ui import localized_ui_text, normalize_ui_language


ROOT = Path(__file__).resolve().parents[1]


class UILocalizationTests(unittest.TestCase):
    def test_preferred_language_normalizes_simplified_chinese_and_falls_back(self):
        self.assertEqual(normalize_ui_language(["zh-Hans-CN", "en-US"]), "zh-Hans")
        self.assertEqual(normalize_ui_language(["zh-CN"]), "zh-Hans")
        self.assertEqual(normalize_ui_language(["fr-FR"]), "en")

    def test_appkit_settings_copy_has_chinese_and_english_fallback(self):
        self.assertEqual(
            localized_ui_text("settings.providers_title", "zh-Hans"),
            "Provider 与显示设置",
        )
        self.assertEqual(
            localized_ui_text("settings.add_provider", "zh-Hans"),
            "添加 Provider",
        )
        self.assertEqual(
            localized_ui_text("settings.add_provider", "en"),
            "Add Provider",
        )

    def test_legacy_step_plan_editor_is_absent_from_settings_ui(self):
        source = (ROOT / "openusage_bar/ui.py").read_text(encoding="utf-8")

        self.assertNotIn("Edit Step Plan", source)
        self.assertNotIn("editStepPlan_", source)
        self.assertNotIn("settings_edit", source)

    def test_swift_apps_ship_complete_chinese_localization_resources(self):
        resources = ROOT / "swift_app/Resources"
        english = (resources / "en.lproj/Localizable.strings").read_text(encoding="utf-8")
        chinese = (resources / "zh-Hans.lproj/Localizable.strings").read_text(encoding="utf-8")

        for key in (
            "Providers", "Connections", "Edit Connection", "Save Changes",
            "Settings", "Today Token", "Usage Details", "Data Health",
        ):
            self.assertIn(f'"{key}" = ', english)
            self.assertIn(f'"{key}" = ', chinese)
        self.assertIn('"Edit Connection" = "编辑连接";', chinese)
        self.assertIn('"Providers" = "Provider";', chinese)

    def test_build_copies_localizations_into_both_swift_app_bundles(self):
        source = (ROOT / "scripts/build_app.sh").read_text(encoding="utf-8")

        self.assertIn('for LANGUAGE in en zh-Hans', source)
        self.assertIn('"$APP/Contents/Resources/$LANGUAGE.lproj"', source)
        self.assertIn('"$ACTIVITY_APP/Contents/Resources/$LANGUAGE.lproj"', source)


if __name__ == "__main__":
    unittest.main()
