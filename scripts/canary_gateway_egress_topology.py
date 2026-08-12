#!/usr/bin/env python3
"""Closed evaluator for an authenticated Gateway egress-attempt window.

This is not native lifecycle or release evidence.  It describes only logical
provider-egress attempts exposed by one authenticated endpoint process epoch;
it does not attribute that endpoint to a service or PID and does not cover the
Observer, other processes, DNS, sockets, HTTP success, or credential stores.
"""

from __future__ import annotations

from dataclasses import dataclass

from openusage_bar.gateway.egress import GatewayEgressAttemptCounters


class GatewayEgressTopologyCanaryError(RuntimeError):
    """A fixed, value-free diagnostic failure."""

    def __init__(self) -> None:
        super().__init__("Gateway egress topology canary failed")


@dataclass(frozen=True, repr=False)
class GatewayEgressTopologySummary:
    """Closed result for one authenticated endpoint process epoch."""

    gateway_egress_attempt_delta_zero: bool

    def __post_init__(self) -> None:
        if self.gateway_egress_attempt_delta_zero is not True:
            raise ValueError("Gateway egress topology summary invalid")

    def __repr__(self) -> str:
        return "<GatewayEgressTopologySummary closed>"


def evaluate_gateway_egress_attempt_window(
    *,
    counters_before: GatewayEgressAttemptCounters,
    counters_after: GatewayEgressAttemptCounters,
) -> GatewayEgressTopologySummary:
    """Accept only one unchanged authenticated endpoint epoch snapshot."""

    if (
        type(counters_before) is not GatewayEgressAttemptCounters
        or type(counters_after) is not GatewayEgressAttemptCounters
        or counters_after != counters_before
    ):
        raise GatewayEgressTopologyCanaryError
    return GatewayEgressTopologySummary(gateway_egress_attempt_delta_zero=True)


__all__ = [
    "GatewayEgressTopologyCanaryError",
    "GatewayEgressTopologySummary",
    "evaluate_gateway_egress_attempt_window",
]
