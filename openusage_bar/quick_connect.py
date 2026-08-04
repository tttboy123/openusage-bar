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


QUICK_CONNECT: dict[str, QuickConnect] = {
    "deepseek": QuickConnect(
        "deepseek", "https://platform.deepseek.com", ("api_key", "auto_detect")
    ),
    "codex": QuickConnect(
        "codex", "https://chatgpt.com/codex", ("oauth", "auto_detect")
    ),
    "minimax": QuickConnect(
        "minimax", "https://platform.minimaxi.com", ("api_key",)
    ),
    "step_plan": QuickConnect(
        "step_plan", "https://platform.stepfun.com", ("api_key", "auto_detect")
    ),
    "moonshot": QuickConnect(
        "moonshot", "https://platform.moonshot.cn", ("api_key", "auto_detect")
    ),
    "omniroute": QuickConnect(
        "omniroute", "http://localhost:20128", ("auto_detect",)
    ),
    "cc_switch": QuickConnect(
        "cc_switch", "https://ccswitch.io", ("auto_detect",)
    ),
}


def quick_connect(family_id: str) -> QuickConnect:
    try:
        return QUICK_CONNECT[family_id]
    except KeyError as error:
        raise ValueError("unknown provider family") from error
