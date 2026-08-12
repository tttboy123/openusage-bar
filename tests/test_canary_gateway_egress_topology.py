from __future__ import annotations

import unittest


class GatewayEgressTopologyCanaryTests(unittest.TestCase):
    def test_evaluator_accepts_only_one_authenticated_endpoint_epoch_with_zero_delta(
        self,
    ) -> None:
        from openusage_bar.gateway.egress import GatewayEgressAttemptCounters
        from scripts.canary_gateway_egress_topology import (
            GatewayEgressTopologySummary,
            evaluate_gateway_egress_attempt_window,
        )

        baseline = GatewayEgressAttemptCounters("a" * 64, 17, 5)

        self.assertEqual(
            evaluate_gateway_egress_attempt_window(
                counters_before=baseline,
                counters_after=baseline,
            ),
            GatewayEgressTopologySummary(gateway_egress_attempt_delta_zero=True),
        )

        for name, counters_after in (
            (
                "process_epoch_restart",
                GatewayEgressAttemptCounters("b" * 64, 17, 5),
            ),
            (
                "network_attempt",
                GatewayEgressAttemptCounters("a" * 64, 18, 5),
            ),
            (
                "credential_attempt",
                GatewayEgressAttemptCounters("a" * 64, 17, 6),
            ),
            (
                "counter_regression",
                GatewayEgressAttemptCounters("a" * 64, 16, 5),
            ),
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "Gateway egress topology canary failed",
                ):
                    evaluate_gateway_egress_attempt_window(
                        counters_before=baseline,
                        counters_after=counters_after,
                    )


if __name__ == "__main__":
    unittest.main()
