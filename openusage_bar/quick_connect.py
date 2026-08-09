"""Provider quick-connect facts for the Provider Center and CLI.

Each family declares a console URL to jump to and the supported connection
modes (manual API key, OAuth, or automatic local detection). This is a
separate stable map so the frozen provider catalog schema stays untouched.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QuickConnect:
    family_id: str
    console_url: str
    auth_modes: tuple[str, ...]
    api_key_url: str | None = None


QUICK_CONNECT: dict[str, QuickConnect] = {
    "deepseek": QuickConnect(
        "deepseek", "https://platform.deepseek.com", ("api_key", "auto_detect"),
        api_key_url="https://platform.deepseek.com/api_keys",
    ),
    "codex": QuickConnect(
        "codex", "https://chatgpt.com/codex", ("oauth", "auto_detect"),
    ),
    "minimax": QuickConnect(
        "minimax", "https://platform.minimaxi.com", ("api_key",),
        api_key_url="https://platform.minimaxi.com/user-center/basic-information/interface-key",
    ),
    "step_plan": QuickConnect(
        "step_plan", "https://platform.stepfun.com", ("api_key", "auto_detect"),
        api_key_url="https://platform.stepfun.com/api-keys",
    ),
    "moonshot": QuickConnect(
        "moonshot", "https://platform.moonshot.cn", ("api_key", "auto_detect"),
        api_key_url="https://platform.moonshot.cn/console/api-keys",
    ),
    "omniroute": QuickConnect(
        "omniroute", "http://localhost:20128", ("auto_detect",)
    ),
   "cc_switch": QuickConnect(
       "cc_switch", "https://ccswitch.io", ("auto_detect",)
   ),
    "openai": QuickConnect(
        "openai", "https://platform.openai.com", ("api_key", "auto_detect"),
        api_key_url="https://platform.openai.com/api-keys",
    ),
    "anthropic": QuickConnect(
        "anthropic", "https://console.anthropic.com", ("api_key", "auto_detect"),
        api_key_url="https://console.anthropic.com/settings/keys",
    ),
    "gemini_api": QuickConnect(
        "gemini_api", "https://aistudio.google.com", ("api_key", "auto_detect"),
        api_key_url="https://aistudio.google.com/app/apikey",
    ),
    "google": QuickConnect(
        "google", "https://aistudio.google.com", ("api_key", "auto_detect"),
        api_key_url="https://aistudio.google.com/app/apikey",
    ),
}


def quick_connect(family_id: str) -> QuickConnect:
    try:
        return QUICK_CONNECT[family_id]
    except KeyError as error:
        raise ValueError("unknown provider family") from error
