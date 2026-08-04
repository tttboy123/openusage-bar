"""Server-owned inference templates for explicitly managed Provider credentials.

The native app may choose a template and model IDs, but it cannot redirect an
existing Provider credential to an arbitrary endpoint.  Only Provider types
whose managed credential is documented for inference are eligible here.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import (
    MiniMaxConfig,
    MoonshotConfig,
    ProviderConfig,
    StepPlanConfig,
)


@dataclass(frozen=True)
class ProviderExecutionTemplate:
    provider_id: str
    family_id: str
    display_name: str
    site: str
    account_ref: str
    fact_account_ref: str | None
    base_url: str
    suggested_models: tuple[str, ...]
    source_credential_account: str


_MINIMAX_ENDPOINTS = {
    "china": "https://api.minimaxi.com/v1",
    "international": "https://api.minimax.io/v1",
}
_MOONSHOT_ENDPOINTS = {
    "china": "https://api.moonshot.cn/v1",
    "international": "https://api.moonshot.ai/v1",
}
_STEP_PLAN_ENDPOINTS = {
    "china": "https://api.stepfun.com/step_plan/v1",
    "international": "https://api.stepfun.ai/step_plan/v1",
}


def template_for_provider(
    configured: ProviderConfig,
) -> ProviderExecutionTemplate | None:
    """Return a fixed inference template, or None for non-inference keys."""
    if isinstance(configured, MiniMaxConfig):
        family_id = "minimax"
        endpoints = _MINIMAX_ENDPOINTS
        models = ("MiniMax-M2.7", "MiniMax-M2.7-highspeed")
    elif isinstance(configured, MoonshotConfig):
        family_id = "moonshot"
        endpoints = _MOONSHOT_ENDPOINTS
        models = ("kimi-k2.5",)
    elif isinstance(configured, StepPlanConfig):
        family_id = "step_plan"
        endpoints = _STEP_PLAN_ENDPOINTS
        models = ("step-3.5-flash", "step-3.5-flash-2603", "step-router-v1")
    else:
        return None
    try:
        base_url = endpoints[configured.site]
    except KeyError:
        return None
    return ProviderExecutionTemplate(
        provider_id=configured.provider_id,
        family_id=family_id,
        display_name=configured.name,
        site=configured.site,
        account_ref=configured.account_ref or configured.provider_id,
        fact_account_ref=configured.account_ref or None,
        base_url=base_url,
        suggested_models=models,
        source_credential_account=configured.provider_id,
    )
