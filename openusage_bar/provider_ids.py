"""Pure OpenUsage provider identity allowlist shared by bounded contracts."""

from __future__ import annotations


OPENUSAGE_PROVIDER_IDS = tuple(
    sorted(
        {
            "openai", "anthropic", "azure_openai", "alibaba_cloud",
            "openrouter", "perplexity", "groq", "mistral", "moonshot",
            "deepseek", "xai", "zai", "gemini_api", "opencode",
            "gemini_cli", "copilot", "cursor", "claude_code", "codex",
            "amp", "goose", "hermes", "mux", "droid", "crush",
            "roocode", "kilo_code", "kiro_cli", "zed", "codebuff",
            "kimi_cli", "openclaw", "pi", "qwen_cli", "ollama",
        }
    )
)


__all__ = ["OPENUSAGE_PROVIDER_IDS"]
