"""Should-Send decisions from recorded quota facts and aggregate telemetry."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite

from .contracts import Decision, ShouldSendDecision, ShouldSendRequest
from ..query import CapacityResult


@dataclass(frozen=True)
class PolicySnapshot:
    remaining_ratio: float | None
    quota_remaining: float | None
    burn_rate_per_minute: float | None
    unknown: bool

    @classmethod
    def from_capacity_result(
        cls,
        capacity: CapacityResult,
        *,
        provider_id: str,
        model_id: str | None = None,
        burn_rate_per_minute: float | None = None,
    ) -> "PolicySnapshot":
        burn_rate = _bounded_burn_rate(burn_rate_per_minute)
        for item in capacity.providers:
            if item.provider_id != provider_id:
                continue
            if model_id is not None and item.applies_to.model_ids:
                if model_id not in item.applies_to.model_ids:
                    continue
            return cls(
                item.remaining_ratio,
                _compatible_token_remaining(item.unit, item.remaining),
                burn_rate,
                item.stale or item.remaining_ratio is None,
            )
        return cls(None, None, burn_rate, True)


class ShouldSendPolicy:
    def __init__(
        self,
        *,
        no_threshold: float = 0.05,
        defer_threshold: float = 0.15,
    ) -> None:
        if not 0 <= no_threshold <= defer_threshold <= 1:
            raise ValueError("policy thresholds must be ordered ratios")
        self.no_threshold = no_threshold
        self.defer_threshold = defer_threshold

    def decide(
        self,
        request: ShouldSendRequest,
        snapshot: PolicySnapshot,
    ) -> ShouldSendDecision:
        ratio = snapshot.remaining_ratio
        predicted = _predicted_exhaustion_minutes(snapshot)
        if (
            snapshot.unknown
            or ratio is None
            or not isfinite(ratio)
            or not 0 <= ratio <= 1
        ):
            return _decision(
                Decision.DEFER,
                0.5,
                "quota_unknown",
                snapshot,
                predicted,
            )
        if ratio <= self.no_threshold:
            return _decision(
                Decision.NO,
                0.95,
                "approaching_limit",
                snapshot,
                predicted,
            )
        if predicted is not None and predicted < 15:
            return _decision(
                Decision.DEFER,
                0.8,
                "burn_rate_too_high",
                snapshot,
                predicted,
            )
        if ratio <= self.defer_threshold:
            return _decision(
                Decision.DEFER,
                0.8,
                "quota_low",
                snapshot,
                predicted,
            )
        return _decision(
            Decision.YES,
            0.92,
            "quota_healthy",
            snapshot,
            predicted,
        )


class SnapshottingShouldSendEvaluator:
    """Bounded 5–15 second fact snapshots for the Should-Send fast path."""

    def __init__(
        self,
        *,
        capacity: Callable[[], CapacityResult],
        burn_rate: Callable[[str, str], float | None] | None = None,
        policy: ShouldSendPolicy | None = None,
        ttl_seconds: float = 10.0,
        max_entries: int = 128,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not callable(capacity)
            or (burn_rate is not None and not callable(burn_rate))
            or (policy is not None and type(policy) is not ShouldSendPolicy)
            or isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not isfinite(float(ttl_seconds))
            or not 5.0 <= float(ttl_seconds) <= 15.0
            or type(max_entries) is not int
            or not 1 <= max_entries <= 1_024
            or not callable(monotonic)
        ):
            raise ValueError("invalid Should-Send evaluator")
        self._capacity = capacity
        self._burn_rate = burn_rate
        self._policy = policy or ShouldSendPolicy()
        self._ttl_seconds = float(ttl_seconds)
        self._max_entries = max_entries
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._snapshots: OrderedDict[
            tuple[str, str], tuple[float, PolicySnapshot]
        ] = OrderedDict()
        self._last_now: float | None = None

    def __call__(self, request: ShouldSendRequest) -> ShouldSendDecision:
        if type(request) is not ShouldSendRequest:
            raise ValueError("invalid Should-Send request")
        snapshot = self._snapshot(request.provider, request.model)
        return self._policy.decide(request, snapshot)

    def clear(self) -> None:
        with self._lock:
            self._snapshots.clear()

    def _snapshot(self, provider_id: str, model_id: str) -> PolicySnapshot:
        key = (provider_id, model_id)
        with self._lock:
            now = self._now()
            cached = self._snapshots.get(key)
            if cached is not None and now < cached[0]:
                self._snapshots.move_to_end(key)
                return cached[1]

            capacity = self._capacity()
            if type(capacity) is not CapacityResult:
                raise ValueError("invalid Should-Send capacity snapshot")
            burn_rate = None
            if self._burn_rate is not None:
                try:
                    burn_rate = self._burn_rate(provider_id, model_id)
                except Exception:
                    burn_rate = None
            snapshot = PolicySnapshot.from_capacity_result(
                capacity,
                provider_id=provider_id,
                model_id=model_id,
                burn_rate_per_minute=burn_rate,
            )
            self._snapshots[key] = (now + self._ttl_seconds, snapshot)
            self._snapshots.move_to_end(key)
            while len(self._snapshots) > self._max_entries:
                self._snapshots.popitem(last=False)
            return snapshot

    def _now(self) -> float:
        try:
            value = self._monotonic()
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
                or float(value) < 0
            ):
                raise ValueError
            now = float(value)
        except Exception:
            raise ValueError("invalid Should-Send clock") from None
        if self._last_now is not None and now < self._last_now:
            self._snapshots.clear()
        self._last_now = now
        return now


def _decision(
    decision: Decision,
    confidence: float,
    reason: str,
    snapshot: PolicySnapshot,
    predicted_exhaustion_minutes: float | None,
) -> ShouldSendDecision:
    return ShouldSendDecision(
        decision=decision,
        confidence=confidence,
        reason=reason,
        defer_until=None,
        quota_remaining=snapshot.quota_remaining,
        burn_rate_per_minute=snapshot.burn_rate_per_minute,
        predicted_exhaustion_minutes=predicted_exhaustion_minutes,
    )


def _predicted_exhaustion_minutes(
    snapshot: PolicySnapshot,
) -> float | None:
    if (
        snapshot.quota_remaining is None
        or snapshot.burn_rate_per_minute is None
        or not isfinite(snapshot.quota_remaining)
        or snapshot.quota_remaining < 0
        or not isfinite(snapshot.burn_rate_per_minute)
        or snapshot.burn_rate_per_minute <= 0
    ):
        return None
    return snapshot.quota_remaining / snapshot.burn_rate_per_minute


def _bounded_burn_rate(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        rate = float(value)
    except (OverflowError, ValueError):
        return None
    if not isfinite(rate) or rate < 0:
        return None
    return rate


def _compatible_token_remaining(unit: object, value: object) -> float | None:
    """Return a finite Token balance only when its unit matches telemetry."""

    if (
        type(unit) is not str
        or unit.strip().casefold() not in {"token", "tokens"}
        or type(value) is not str
    ):
        return None
    try:
        remaining = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not isfinite(remaining) or remaining < 0:
        return None
    return remaining
