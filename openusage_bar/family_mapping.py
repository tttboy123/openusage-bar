"""Read-time provider/model family normalization for presentation surfaces.

The ledger records provider identity as observed: session sources such as
``codex.local_sessions``, or legacy account names such as
``deepseek-primary``. Presentation surfaces aggregate by model family: a
DeepSeek model used through a Codex session still belongs to the DeepSeek
family. This module maps observed identities to a stable family id without
rewriting ledger rows.
"""

from __future__ import annotations

PROVIDER_FAMILY_ALIASES: dict[str, str] = {
    "deepseek-primary": "deepseek",
}

MODEL_FAMILY_PREFIXES: dict[str, tuple[str, ...]] = {
    "deepseek": ("deepseek-", "deepseek_", "deepseek."),
    "moonshot": ("kimi-", "kimi_", "kimi."),
}


def resolve_family(provider_id: str, model_id: str | None = None) -> str:
    """Resolve the display family for an observed provider/model pair.

    A model family prefix wins over a provider alias: a ``deepseek-*``
    model observed in a Codex session belongs to DeepSeek. Without a model
    match, provider aliases normalize legacy account names. The raw provider
    id is the fallback so unknown identities stay stable.
    """
    if model_id:
        lowered = model_id.lower()
        for family, prefixes in MODEL_FAMILY_PREFIXES.items():
            if any(lowered.startswith(prefix) for prefix in prefixes):
                return family
    return PROVIDER_FAMILY_ALIASES.get(provider_id, provider_id)
