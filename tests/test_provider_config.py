from __future__ import annotations

import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from openusage_bar.provider_config import (
    AGENTS,
    AgentConfigResult,
    apply_provider_config,
    agent_config_path,
    find_preset,
    load_presets,
    render_claude_settings,
    render_codex_toml,
    render_gemini_settings,
    render_opencode_config,
)


class ProviderConfigPresetTests(unittest.TestCase):
    def test_catalog_loads_all_presets_with_strict_schema(self) -> None:
        presets = load_presets()
        self.assertGreaterEqual(len(presets), 12)
        ids = [preset.preset_id for preset in presets]
        self.assertEqual(len(ids), len(set(ids)))
        for preset in presets:
            with self.subTest(preset=preset.preset_id):
                self.assertIn(preset.category, {"official", "gateway"})
                self.assertIn(preset.agent, AGENTS)
                self.assertTrue(preset.console_url.startswith("https://"))
                if preset.api_key_url is not None:
                    self.assertTrue(preset.api_key_url.startswith("https://"))
                self.assertIsNotNone(preset.template_value("base_url"))
                self.assertIsNotNone(preset.template_value("model"))
                self.assertIs(type(preset.allow_custom_endpoints), bool)
        self.assertEqual(
            {preset.agent for preset in presets},
            {"claude_code", "codex", "gemini_cli", "opencode"},
        )

    def test_every_agent_has_an_official_preset(self) -> None:
        presets = load_presets()
        by_agent: dict[str, set[str]] = {}
        for preset in presets:
            by_agent.setdefault(preset.agent, set()).add(preset.category)
        for agent in AGENTS:
            self.assertIn("official", by_agent[agent], agent)

    def test_find_preset_rejects_unknown_identity(self) -> None:
        presets = load_presets()
        with self.assertRaisesRegex(ValueError, "unknown"):
            find_preset(presets, "no-such-preset")


class ProviderConfigWriterTests(unittest.TestCase):
    def test_claude_settings_writes_env_and_preserves_existing(self) -> None:
        existing = json.dumps({"apiKeyHelper": "off", "env": {"HTTP_PROXY": "http://proxy"}})
        rendered = render_claude_settings(
            existing,
            base_url="https://api.anthropic.com",
            api_key="sk-test",
            model="claude-sonnet-5",
        )
        payload = json.loads(rendered)
        self.assertEqual(payload["apiKeyHelper"], "off")
        self.assertEqual(payload["env"]["HTTP_PROXY"], "http://proxy")
        self.assertEqual(payload["env"]["ANTHROPIC_BASE_URL"], "https://api.anthropic.com")
        self.assertEqual(payload["env"]["ANTHROPIC_AUTH_TOKEN"], "sk-test")
        self.assertEqual(payload["env"]["ANTHROPIC_MODEL"], "claude-sonnet-5")

    def test_gemini_settings_writes_env_and_preserves_existing(self) -> None:
        existing = json.dumps({"GOOGLE_AUTH_MODE": "api_key"})
        rendered = render_gemini_settings(
            existing,
            base_url="https://generativelanguage.googleapis.com",
            api_key="ai-test",
            model="gemini-2.5-pro",
        )
        payload = json.loads(rendered)
        self.assertEqual(payload["GOOGLE_AUTH_MODE"], "api_key")
        self.assertEqual(payload["GOOGLE_API_KEY"], "ai-test")
        self.assertEqual(payload["GOOGLE_GEMINI_MODEL"], "gemini-2.5-pro")

    def test_codex_toml_writes_provider_block_and_round_trips(self) -> None:
        existing = "model = \"old\"\n[model_providers.other]\nname = \"Other\"\n"
        rendered = render_codex_toml(
            existing,
            provider_id="openai",
            base_url="https://api.openai.com/v1",
            env_key="OPENAI_API_KEY",
            model="gpt-5.5",
        )
        parsed = tomllib.loads(rendered)
        self.assertEqual(parsed["model"], "gpt-5.5")
        self.assertEqual(parsed["model_provider"], "openai")
        self.assertEqual(parsed["model_providers"]["openai"]["base_url"], "https://api.openai.com/v1")
        self.assertEqual(parsed["model_providers"]["openai"]["env_key"], "OPENAI_API_KEY")
        # Unrelated existing provider is preserved.
        self.assertEqual(parsed["model_providers"]["other"]["name"], "Other")

    def test_opencode_config_writes_provider_and_preserves_mcp(self) -> None:
        existing = json.dumps({"mcp": {"codebase-memory-mcp": {"enabled": True}}})
        rendered = render_opencode_config(
            existing,
            provider_id="deepseek",
            npm="@ai-sdk/openai-compatible",
            base_url="https://api.deepseek.com/v1",
            api_key="ds-test",
            model="deepseek-chat",
        )
        payload = json.loads(rendered)
        self.assertIn("mcp", payload)
        provider = payload["provider"]["deepseek"]
        self.assertEqual(provider["options"]["baseURL"], "https://api.deepseek.com/v1")
        self.assertEqual(provider["options"]["apiKey"], "ds-test")
        self.assertIn("deepseek-chat", provider["models"])
        self.assertEqual(payload["model"], "deepseek/deepseek-chat")

    def test_apply_writes_each_agent_config_and_round_trips(self) -> None:
        presets = load_presets()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            seen_agents: set[str] = set()
            for preset in presets:
                with self.subTest(preset=preset.preset_id):
                    result = apply_provider_config(
                        preset_id=preset.preset_id,
                        api_key="sk-secret-value",
                        home=home,
                        presets=presets,
                    )
                    self.assertIsInstance(result, AgentConfigResult)
                    self.assertEqual(result.preset_id, preset.preset_id)
                    self.assertEqual(result.agent, preset.agent)
                    if preset.agent in seen_agents:
                        self.assertEqual(result.status, "updated")
                    else:
                        self.assertEqual(result.status, "created")
                        seen_agents.add(preset.agent)
                    path = agent_config_path(home, preset.agent)
                    self.assertTrue(path.is_file())
                    self.assertNotIn("sk-secret-value", str(result))
                    # Apply the same preset again -> updated, still valid.
                    second = apply_provider_config(
                        preset_id=preset.preset_id,
                        api_key="sk-secret-value",
                        home=home,
                        presets=presets,
                    )
                    self.assertEqual(second.status, "updated")

    def test_apply_with_custom_endpoint_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            result = apply_provider_config(
                preset_id="claude-anthropic-official",
                api_key="sk-custom",
                base_url="https://gateway.example.test",
                model="claude-opus-5",
                home=home,
            )
            self.assertEqual(result.status, "created")
            payload = json.loads(agent_config_path(home, "claude_code").read_text())
            self.assertEqual(payload["env"]["ANTHROPIC_BASE_URL"], "https://gateway.example.test")
            self.assertEqual(payload["env"]["ANTHROPIC_MODEL"], "claude-opus-5")

    def test_apply_rejects_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with self.assertRaisesRegex(ValueError, "unknown"):
                apply_provider_config(preset_id="nope", api_key="sk-x", home=home)
            with self.assertRaisesRegex(ValueError, "API key is required"):
                apply_provider_config(preset_id="claude-anthropic-official", api_key="", home=home)
            with self.assertRaisesRegex(ValueError, "too large"):
                apply_provider_config(
                    preset_id="claude-anthropic-official",
                    api_key="sk-" + "x" * 5000,
                    home=home,
                )
            with self.assertRaisesRegex(ValueError, "control"):
                apply_provider_config(
                    preset_id="claude-anthropic-official",
                    api_key="sk-ok",
                    base_url="https://x.test\nEVIL",
                    home=home,
                )
            with self.assertRaisesRegex(ValueError, "absolute"):
                apply_provider_config(
                    preset_id="claude-anthropic-official",
                    api_key="sk-ok",
                    home=Path("relative/home"),
                )

    def test_codex_toml_preserves_quoted_keys_and_fails_closed_on_bad_input(self) -> None:
        existing = (
            'model = "deepseek-v4-flash"\n'
            'model_provider = "custom"\n'
            '[plugins."sites@openai-bundled"]\n'
            'enabled = true\n'
            '[projects."/Users/lune"]\n'
            'trust_level = "trusted"\n'
        )
        rendered = render_codex_toml(
            existing,
            provider_id="openai",
            base_url="https://api.openai.com/v1",
            env_key="OPENAI_API_KEY",
            model="gpt-5.5",
        )
        parsed = tomllib.loads(rendered)
        self.assertEqual(parsed["plugins"]["sites@openai-bundled"]["enabled"], True)
        self.assertEqual(parsed["projects"]["/Users/lune"]["trust_level"], "trusted")
        self.assertEqual(parsed["model_providers"]["openai"]["base_url"], "https://api.openai.com/v1")
        with self.assertRaisesRegex(ValueError, "valid TOML"):
            render_codex_toml(
                "model = ",
                provider_id="openai",
                base_url="https://api.openai.com/v1",
                env_key="OPENAI_API_KEY",
                model="gpt-5.5",
            )

    def test_apply_never_leaks_secret_in_result_or_error(self) -> None:
        secret = "sk-super-secret-9f3a2"
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            result = apply_provider_config(
                preset_id="opencode-deepseek-official",
                api_key=secret,
                home=home,
            )
            self.assertNotIn(secret, str(result))
            serialized = json.dumps(result.__dict__)
            self.assertNotIn(secret, serialized)
            written = agent_config_path(home, "opencode").read_text()
            self.assertIn(secret, written)


if __name__ == "__main__":
    unittest.main()


class ProviderConfigWebSyncTests(unittest.TestCase):
    def test_web_preset_copy_matches_python_resource(self) -> None:
        import json as _json
        from pathlib import Path as _Path

        root = _Path(__file__).resolve().parents[1]
        resource = _json.loads(
            (root / "openusage_bar/resources/provider-config-presets.v1.json").read_text()
        )
        web_copy = _json.loads(
            (root / "web/public/provider-config-presets.json").read_text()
        )
        self.assertEqual(web_copy["schemaVersion"], 1)
        web_by_id = {p["presetId"]: p for p in web_copy["presets"]}
        self.assertEqual(len(web_by_id), len(web_copy["presets"]))
        for preset in resource["presets"]:
            with self.subTest(preset=preset["preset_id"]):
                rendered = web_by_id[preset["preset_id"]]
                self.assertEqual(rendered["name"], preset["name"])
                self.assertEqual(rendered["category"], preset["category"])
                self.assertEqual(rendered["agent"], preset["agent"])
                self.assertEqual(rendered["familyId"], preset["family_id"])
                self.assertEqual(rendered["consoleUrl"], preset["console_url"])
                self.assertEqual(
                    rendered["apiKeyUrl"], preset.get("api_key_url")
                )
                self.assertEqual(
                    rendered["baseUrl"], preset["template"]["base_url"]
                )
                self.assertEqual(rendered["model"], preset["template"]["model"])
                self.assertEqual(
                    rendered["allowCustomEndpoints"],
                    preset["allow_custom_endpoints"],
                )
