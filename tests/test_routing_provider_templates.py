from __future__ import annotations

import unittest

from openusage_bar.config import (
    GenericProviderConfig,
    MiniMaxConfig,
    MoonshotConfig,
    OpenAIOrganizationConfig,
    StepPlanConfig,
)
from openusage_bar.routing_provider_templates import template_for_provider


class RoutingProviderTemplateTests(unittest.TestCase):
    def test_managed_inference_keys_map_to_site_locked_openai_compatible_endpoints(self) -> None:
        cases = [
            (
                MiniMaxConfig("minimax-cn", "MiniMax CN", site="china"),
                "https://api.minimaxi.com/v1",
                ("MiniMax-M2.7", "MiniMax-M2.7-highspeed"),
            ),
            (
                MiniMaxConfig("minimax-global", "MiniMax Global", site="international"),
                "https://api.minimax.io/v1",
                ("MiniMax-M2.7", "MiniMax-M2.7-highspeed"),
            ),
            (
                MoonshotConfig("kimi-cn", "Kimi CN", site="china"),
                "https://api.moonshot.cn/v1",
                ("kimi-k2.5",),
            ),
            (
                MoonshotConfig("kimi-global", "Kimi Global", site="international"),
                "https://api.moonshot.ai/v1",
                ("kimi-k2.5",),
            ),
            (
                StepPlanConfig("step-cn", "Step CN", site="china"),
                "https://api.stepfun.com/step_plan/v1",
                ("step-3.5-flash", "step-3.5-flash-2603", "step-router-v1"),
            ),
            (
                StepPlanConfig("step-global", "Step Global", site="international"),
                "https://api.stepfun.ai/step_plan/v1",
                ("step-3.5-flash", "step-3.5-flash-2603", "step-router-v1"),
            ),
        ]
        for configured, endpoint, models in cases:
            with self.subTest(provider=configured.provider_id):
                template = template_for_provider(configured)
                self.assertIsNotNone(template)
                assert template is not None
                self.assertEqual(template.base_url, endpoint)
                self.assertEqual(template.suggested_models, models)
                self.assertEqual(template.source_credential_account, configured.provider_id)
                self.assertEqual(
                    template.account_ref,
                    configured.account_ref or configured.provider_id,
                )
                self.assertEqual(
                    template.fact_account_ref,
                    configured.account_ref or None,
                )

    def test_billing_admin_and_generic_keys_are_never_assumed_to_be_inference_keys(self) -> None:
        excluded = [
            OpenAIOrganizationConfig("openai-admin", "OpenAI Admin"),
            GenericProviderConfig(
                "custom-quota", "Custom quota", "https://example.com/quota",
                "Authorization", "Bearer ", "remaining",
            ),
        ]
        for configured in excluded:
            with self.subTest(provider=configured.provider_id):
                self.assertIsNone(template_for_provider(configured))


if __name__ == "__main__":
    unittest.main()
