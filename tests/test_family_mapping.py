import unittest

from openusage_bar.family_mapping import resolve_family


class FamilyMappingTests(unittest.TestCase):
    def test_legacy_provider_alias_normalizes_to_deepseek(self):
        self.assertEqual(resolve_family("deepseek-primary"), "deepseek")
        self.assertEqual(resolve_family("deepseek-primary", "deepseek-chat-v3"), "deepseek")

    def test_deepseek_model_in_codex_session_belongs_to_deepseek_family(self):
        self.assertEqual(resolve_family("codex", "deepseek-v4-flash"), "deepseek")
        self.assertEqual(resolve_family("codex", "deepseek_v4_pro"), "deepseek")
        self.assertEqual(resolve_family("codex", "deepseek.chat"), "deepseek")

    def test_unknown_model_family_prefix_resolves_moonshot(self):
        self.assertEqual(resolve_family("unknown_source", "kimi-k3"), "moonshot")

    def test_plain_model_keeps_provider_family(self):
        self.assertEqual(resolve_family("codex", "gpt-5.5"), "codex")
        self.assertEqual(resolve_family("minimax", "MiniMax-M3-512k"), "minimax")

    def test_unknown_identity_stays_stable(self):
        self.assertEqual(resolve_family("mystery_provider"), "mystery_provider")
        self.assertEqual(resolve_family("mystery_provider", "custom-model"), "mystery_provider")


if __name__ == "__main__":
    unittest.main()
