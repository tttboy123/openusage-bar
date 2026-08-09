"""Bounded, deterministic Gateway fallback policy.

The module deliberately contains no transport code.  It accepts only stable
provider/model identifiers and aggregate health facts, and returns a selection
decision that the runtime may execute.  Provider error bodies, prompts, and
responses never cross this boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
import math
import threading
import time
import unicodedata

from .contracts import Decision


_PROVIDER_IDS = frozenset(
    {"anthropic", "deepseek", "ollama", "openai", "openrouter"}
)
_MAX_MODEL_LENGTH = 256
_MAX_CANDIDATES = len(_PROVIDER_IDS)
_MAX_FAILURE_THRESHOLD = 10_000
_MAX_RESET_SECONDS = 24 * 60 * 60
_MAX_COST = 1_000_000_000.0
_MAX_COST_MULTIPLIER = 1_000.0
_MAX_RETRIES = 16
_HEALTH_WINDOW_SECONDS = 5 * 60
_MAX_HEALTH_EVENTS_PER_PROVIDER = 4_096
_MAX_LATENCY_MS = 60 * 60 * 1_000


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class HealthStatus(str, Enum):
    SUCCESS = "success"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"


class FailureSignal(str, Enum):
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    QUOTA_EXHAUSTED = "quota_exhausted"
    STREAM_INTERRUPTED = "stream_interrupted"
    CLIENT_ABORT = "client_abort"
    POLICY_REJECTED = "policy_rejected"


class FallbackAction(str, Enum):
    RETRY = "retry"
    QUEUE = "queue"
    FAIL = "fail"
    DEGRADE_TO_CHEAP = "degrade_to_cheap"


def _valid_finite_number(value: object) -> float | None:
    if type(value) not in {int, float}:
        return None
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return normalized if math.isfinite(normalized) else None


def _valid_provider_id(value: object) -> str | None:
    if type(value) is str and value in _PROVIDER_IDS:
        return value
    return None


def _valid_model(value: object) -> str | None:
    if type(value) is not str or not value or len(value) > _MAX_MODEL_LENGTH:
        return None
    if value != value.strip():
        return None
    if any(unicodedata.category(character).startswith("C") for character in value):
        return None
    try:
        if len(value.encode("utf-8")) > _MAX_MODEL_LENGTH * 4:
            return None
    except UnicodeEncodeError:
        return None
    return value


def _valid_cost(value: object) -> float | None:
    normalized = _valid_finite_number(value)
    if normalized is None or not 0 <= normalized <= _MAX_COST:
        return None
    return normalized


@dataclass(frozen=True, repr=False, slots=True)
class Candidate:
    """One already-normalized provider/model fallback candidate."""

    provider_id: str
    model: str
    estimated_cost: float
    should_send: Decision
    breaker_state: BreakerState = BreakerState.CLOSED
    health_score: float = 1.0

    def __post_init__(self) -> None:
        cost = _valid_cost(self.estimated_cost)
        score = _valid_finite_number(self.health_score)
        if (
            _valid_provider_id(self.provider_id) is None
            or _valid_model(self.model) is None
            or cost is None
            or type(self.should_send) is not Decision
            or type(self.breaker_state) is not BreakerState
            or score is None
            or not 0 <= score <= 1
        ):
            raise ValueError("invalid fallback candidate")
        object.__setattr__(self, "estimated_cost", cost)
        object.__setattr__(self, "health_score", score)

    def __repr__(self) -> str:
        try:
            _validate_candidate(self)
        except Exception:
            return "Candidate(<invalid>)"
        return "Candidate(provider_id=<redacted>, model=<redacted>)"


@dataclass(frozen=True, repr=False, slots=True)
class HealthEvent:
    """A payload-free observation used by the five-minute health window."""

    provider_id: str
    status: HealthStatus
    latency_ms: float | None
    observed_at: float

    def __post_init__(self) -> None:
        observed_at = _valid_finite_number(self.observed_at)
        latency = (
            None
            if self.latency_ms is None
            else _valid_finite_number(self.latency_ms)
        )
        if (
            _valid_provider_id(self.provider_id) is None
            or type(self.status) is not HealthStatus
            or observed_at is None
            or (
                self.latency_ms is not None
                and (
                    latency is None
                    or not 0 <= latency <= _MAX_LATENCY_MS
                )
            )
        ):
            raise ValueError("invalid health event")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "latency_ms", latency)

    def __repr__(self) -> str:
        try:
            _validate_health_event(self)
        except Exception:
            return "HealthEvent(<invalid>)"
        return "HealthEvent(provider_id=<redacted>)"


@dataclass(frozen=True, slots=True)
class _HealthSnapshotState:
    provider_id: str
    window_seconds: int
    sample_count: int
    success_rate: float
    p50_latency_ms: float | None
    p99_latency_ms: float | None
    rate_limit_count: int
    server_error_count: int
    timeout_count: int
    health_score: float


@dataclass(frozen=True, repr=False, slots=True)
class HealthSnapshot:
    provider_id: str
    window_seconds: int
    sample_count: int
    success_rate: float
    p50_latency_ms: float | None
    p99_latency_ms: float | None
    rate_limit_count: int
    server_error_count: int
    timeout_count: int
    health_score: float

    def __post_init__(self) -> None:
        state = _validated_health_snapshot_state(self)
        object.__setattr__(self, "success_rate", state.success_rate)
        object.__setattr__(self, "health_score", state.health_score)
        object.__setattr__(self, "p50_latency_ms", state.p50_latency_ms)
        object.__setattr__(self, "p99_latency_ms", state.p99_latency_ms)

    def to_public_dict(self) -> dict[str, object]:
        # Frozen dataclasses can still be changed with ``object.__setattr__``.
        # Revalidate at every serialization boundary so corruption can never
        # turn this aggregate-only view into a disclosure channel.
        state = _validated_health_snapshot_state(self)
        return {
            "providerId": state.provider_id,
            "windowSeconds": state.window_seconds,
            "sampleCount": state.sample_count,
            "successRate": state.success_rate,
            "p50LatencyMs": state.p50_latency_ms,
            "p99LatencyMs": state.p99_latency_ms,
            "rateLimitCount": state.rate_limit_count,
            "serverErrorCount": state.server_error_count,
            "timeoutCount": state.timeout_count,
            "healthScore": state.health_score,
        }

    def __repr__(self) -> str:
        try:
            _validated_health_snapshot_state(self)
        except Exception:
            return "HealthSnapshot(<invalid>)"
        return "HealthSnapshot(<sanitized>)"


@dataclass(frozen=True, repr=False, slots=True)
class ReplayState:
    output_delta_observed: bool = False
    tool_call_observed: bool = False
    provider_side_effect_observed: bool = False
    client_aborted: bool = False

    def __post_init__(self) -> None:
        if any(
            type(value) is not bool
            for value in (
                self.output_delta_observed,
                self.tool_call_observed,
                self.provider_side_effect_observed,
                self.client_aborted,
            )
        ):
            raise ValueError("invalid replay state")

    @property
    def replay_safe(self) -> bool:
        return not (
            self.output_delta_observed
            or self.tool_call_observed
            or self.provider_side_effect_observed
            or self.client_aborted
        )

    def __repr__(self) -> str:
        try:
            _validate_replay_state(self)
        except Exception:
            return "ReplayState(<invalid>)"
        return "ReplayState(<sanitized>)"


@dataclass(frozen=True, slots=True)
class _FallbackDecisionState:
    action: FallbackAction
    candidate: Candidate | None
    error_code: str
    retryable: bool


@dataclass(frozen=True, repr=False, slots=True)
class FallbackDecision:
    action: FallbackAction
    candidate: Candidate | None
    error_code: str
    retryable: bool

    def __post_init__(self) -> None:
        _validated_fallback_decision_state(self)

    def to_public_dict(self) -> dict[str, object]:
        # Candidate identifiers remain available to the in-process runtime via
        # ``candidate``.  The public form intentionally exposes only state.
        state = _validated_fallback_decision_state(self)
        return {
            "action": state.action.value,
            "hasCandidate": state.candidate is not None,
            "errorCode": state.error_code,
            "retryable": state.retryable,
        }

    def __repr__(self) -> str:
        try:
            _validated_fallback_decision_state(self)
        except Exception:
            return "FallbackDecision(<invalid>)"
        return "FallbackDecision(<sanitized>)"


class CircuitBreaker:
    """Thread-safe closed/open/half-open breaker with one probe slot."""

    __slots__ = (
        "_failure_count",
        "_failure_threshold",
        "_last_now",
        "_lock",
        "_opened_at",
        "_probe_in_flight",
        "_reset_after_seconds",
    )

    def __init__(
        self,
        *,
        failure_threshold: int,
        reset_after_seconds: float,
    ) -> None:
        reset = _valid_finite_number(reset_after_seconds)
        if (
            type(failure_threshold) is not int
            or not 1 <= failure_threshold <= _MAX_FAILURE_THRESHOLD
            or reset is None
            or not 0 < reset <= _MAX_RESET_SECONDS
        ):
            raise ValueError("invalid circuit breaker configuration")
        self._failure_threshold = failure_threshold
        self._reset_after_seconds = reset
        self._failure_count = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._last_now: float | None = None
        self._lock = threading.Lock()

    def state(self, *, now: float | None = None) -> BreakerState:
        with self._lock:
            current = self._safe_now(now)
            if current is None:
                return BreakerState.OPEN
            return self._state_at(current)

    def allow_request(self, *, now: float | None = None) -> bool:
        with self._lock:
            current = self._safe_now(now)
            if current is None:
                return False
            state = self._state_at(current)
            if state is BreakerState.CLOSED:
                return True
            if state is BreakerState.OPEN or self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    def record_failure(self, *, now: float | None = None) -> None:
        with self._lock:
            current = self._safe_now(now)
            if current is None:
                return
            state = self._state_at(current)
            if state is BreakerState.CLOSED:
                self._failure_count += 1
                if self._failure_count < self._failure_threshold:
                    return
            self._open(current)

    def record_success(self, *, now: float | None = None) -> None:
        with self._lock:
            current = self._safe_now(now)
            if current is None:
                return
            state = self._state_at(current)
            if state is BreakerState.OPEN:
                return
            if state is BreakerState.HALF_OPEN and not self._probe_in_flight:
                return
            self._failure_count = 0
            self._opened_at = None
            self._probe_in_flight = False

    def _safe_now(self, now: float | None) -> float | None:
        raw = time.monotonic() if now is None else now
        current = _valid_finite_number(raw)
        if current is None:
            self._force_open_on_clock_failure()
            return None
        if self._last_now is not None and current < self._last_now:
            self._open(self._last_now)
            return None
        self._last_now = current
        return current

    def _force_open_on_clock_failure(self) -> None:
        if self._last_now is None:
            # No finite timestamp exists yet.  A later valid timestamp becomes
            # the start of a complete reset interval before a probe is allowed.
            self._failure_count = self._failure_threshold
            self._opened_at = None
            self._probe_in_flight = True
            return
        self._open(self._last_now)

    def _state_at(self, now: float) -> BreakerState:
        if self._opened_at is None:
            if self._failure_count >= self._failure_threshold:
                # Recover deterministically after an invalid initial clock.
                self._opened_at = now
                self._probe_in_flight = False
                return BreakerState.OPEN
            return BreakerState.CLOSED
        if now - self._opened_at >= self._reset_after_seconds:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def _open(self, now: float) -> None:
        self._failure_count = self._failure_threshold
        self._opened_at = now
        self._probe_in_flight = False


class HealthWindow:
    """A bounded in-memory five-minute window of payload-free events."""

    __slots__ = ("_events", "_last_snapshot_now", "_lock", "window_seconds")

    def __init__(self, *, window_seconds: int = _HEALTH_WINDOW_SECONDS) -> None:
        if (
            type(window_seconds) is not int
            or window_seconds != _HEALTH_WINDOW_SECONDS
        ):
            raise ValueError("health window must be five minutes")
        self.window_seconds = window_seconds
        self._events: dict[str, list[HealthEvent]] = {}
        self._last_snapshot_now: float | None = None
        self._lock = threading.Lock()

    def record(self, event: HealthEvent) -> None:
        if type(event) is not HealthEvent:
            raise ValueError("invalid health event")
        # Revalidate because frozen dataclasses can still be tampered with via
        # object.__setattr__ by an untrusted in-process caller.
        _validate_health_event(event)
        with self._lock:
            events = self._events.setdefault(event.provider_id, [])
            if len(events) >= _MAX_HEALTH_EVENTS_PER_PROVIDER:
                raise ValueError("health window capacity exceeded")
            events.append(event)

    def snapshot(self, provider_id: str, *, now: float) -> HealthSnapshot:
        if _valid_provider_id(provider_id) is None:
            raise ValueError("invalid health provider")
        current = _valid_finite_number(now)
        if current is None:
            raise ValueError("invalid health clock")
        with self._lock:
            if (
                self._last_snapshot_now is not None
                and current < self._last_snapshot_now
            ):
                current = self._last_snapshot_now
            self._last_snapshot_now = current
            cutoff = current - self.window_seconds
            all_events = self._events.get(provider_id, [])
            events = [
                event
                for event in all_events
                if cutoff <= event.observed_at <= current
            ]
            if all_events:
                # Snapshot time is the only trusted pruning watermark.  This
                # prevents a future or out-of-order event from evicting a valid
                # current-window observation during ``record``.
                self._events[provider_id] = list(events)
        return _health_snapshot(provider_id, self.window_seconds, events)


class FallbackSelector:
    """Apply safety gates first, then deterministic health/sticky ranking."""

    __slots__ = ("cost_cap_multiplier", "max_retries", "on_exhausted")

    def __init__(
        self,
        *,
        cost_cap_multiplier: float,
        max_retries: int = 1,
        on_exhausted: FallbackAction = FallbackAction.FAIL,
    ) -> None:
        multiplier = _valid_finite_number(cost_cap_multiplier)
        if (
            multiplier is None
            or not 1 <= multiplier <= _MAX_COST_MULTIPLIER
            or type(max_retries) is not int
            or not 0 <= max_retries <= _MAX_RETRIES
            or type(on_exhausted) is not FallbackAction
            or on_exhausted is FallbackAction.RETRY
        ):
            raise ValueError("invalid fallback configuration")
        self.cost_cap_multiplier = multiplier
        self.max_retries = max_retries
        self.on_exhausted = on_exhausted

    def select(
        self,
        *,
        primary_cost: float,
        candidates: Iterable[Candidate],
        sticky_provider_id: str | None = None,
    ) -> Candidate | None:
        cost = _valid_cost(primary_cost)
        if cost is None:
            raise ValueError("invalid fallback selection")
        if (
            sticky_provider_id is not None
            and _valid_provider_id(sticky_provider_id) is None
        ):
            raise ValueError("invalid fallback selection")
        normalized = _bounded_candidates(candidates)

        # Gate order is deliberate: breaker -> health validity -> Should-Send
        # -> configured cost cap.  Sticky affinity is only a final tie-breaker.
        eligible: list[Candidate] = []
        cost_cap = cost * self.cost_cap_multiplier
        for item in normalized:
            if item.breaker_state is BreakerState.OPEN:
                continue
            if not 0 <= item.health_score <= 1:
                continue
            if item.should_send is not Decision.YES:
                continue
            if item.estimated_cost > cost_cap:
                continue
            eligible.append(item)
        if not eligible:
            return None

        best_health = max(item.health_score for item in eligible)
        healthiest = [item for item in eligible if item.health_score == best_health]
        if sticky_provider_id is not None:
            for item in healthiest:
                if item.provider_id == sticky_provider_id:
                    return item
        return healthiest[0]

    def decide(
        self,
        *,
        failure: FailureSignal,
        retries_used: int,
        primary_cost: float,
        candidates: Iterable[Candidate],
        sticky_provider_id: str | None = None,
        replay_state: ReplayState | None = None,
        cheap_candidate: Candidate | None = None,
    ) -> FallbackDecision:
        if (
            type(failure) is not FailureSignal
            or type(retries_used) is not int
            or not 0 <= retries_used <= _MAX_RETRIES
            or (replay_state is not None and type(replay_state) is not ReplayState)
            or (cheap_candidate is not None and type(cheap_candidate) is not Candidate)
        ):
            raise ValueError("invalid fallback decision")
        state = replay_state if replay_state is not None else ReplayState()
        _validate_replay_state(state)
        normalized = _bounded_candidates(candidates)
        if _valid_cost(primary_cost) is None:
            raise ValueError("invalid fallback decision")
        if (
            sticky_provider_id is not None
            and _valid_provider_id(sticky_provider_id) is None
        ):
            raise ValueError("invalid fallback decision")
        if cheap_candidate is not None:
            _validate_candidate(cheap_candidate)
        error_code = _FAILURE_ERROR_CODES[failure]

        if failure in {
            FailureSignal.CLIENT_ABORT,
            FailureSignal.STREAM_INTERRUPTED,
        }:
            return FallbackDecision(
                FallbackAction.FAIL,
                None,
                error_code,
                False,
            )

        # Once externally visible behavior may have happened, no automatic
        # replay and no queue is safe, regardless of configured exhaustion mode.
        if not state.replay_safe:
            return FallbackDecision(
                FallbackAction.FAIL,
                None,
                "stream_interrupted" if state.client_aborted else error_code,
                False,
            )
        if retries_used >= self.max_retries:
            return _exhausted(FallbackAction.FAIL)

        selected = self.select(
            primary_cost=primary_cost,
            candidates=normalized,
            sticky_provider_id=sticky_provider_id,
        )
        if selected is not None:
            return FallbackDecision(
                FallbackAction.RETRY,
                selected,
                error_code,
                True,
            )

        if self.on_exhausted is FallbackAction.DEGRADE_TO_CHEAP:
            cheap = (
                None
                if cheap_candidate is None
                else self.select(
                    primary_cost=primary_cost,
                    candidates=(cheap_candidate,),
                )
            )
            if cheap is None:
                return _exhausted(FallbackAction.FAIL)
            return FallbackDecision(
                FallbackAction.DEGRADE_TO_CHEAP,
                cheap,
                "fallback_exhausted",
                True,
            )
        return _exhausted(self.on_exhausted)


_FAILURE_ERROR_CODES: dict[FailureSignal, str] = {
    FailureSignal.RATE_LIMIT: "upstream_rate_limited",
    FailureSignal.SERVER_ERROR: "upstream_server_error",
    FailureSignal.TIMEOUT: "upstream_timeout",
    FailureSignal.QUOTA_EXHAUSTED: "policy_rejected",
    FailureSignal.STREAM_INTERRUPTED: "stream_interrupted",
    FailureSignal.CLIENT_ABORT: "stream_interrupted",
    FailureSignal.POLICY_REJECTED: "policy_rejected",
}
_SANITIZED_ERROR_CODES = frozenset(
    {"fallback_exhausted", *_FAILURE_ERROR_CODES.values()}
)


def _validated_health_snapshot_state(
    snapshot: HealthSnapshot,
) -> _HealthSnapshotState:
    try:
        provider_id = snapshot.provider_id
        window_seconds = snapshot.window_seconds
        sample_count = snapshot.sample_count
        success_rate = _valid_finite_number(snapshot.success_rate)
        p50_source = snapshot.p50_latency_ms
        p99_source = snapshot.p99_latency_ms
        rate_limit_count = snapshot.rate_limit_count
        server_error_count = snapshot.server_error_count
        timeout_count = snapshot.timeout_count
        health_score = _valid_finite_number(snapshot.health_score)
    except Exception:
        raise ValueError("invalid health snapshot") from None

    p50 = (
        None if p50_source is None else _valid_finite_number(p50_source)
    )
    p99 = (
        None if p99_source is None else _valid_finite_number(p99_source)
    )
    counts = (
        sample_count,
        rate_limit_count,
        server_error_count,
        timeout_count,
    )
    if (
        _valid_provider_id(provider_id) is None
        or type(window_seconds) is not int
        or window_seconds != _HEALTH_WINDOW_SECONDS
        or any(
            type(value) is not int
            or not 0 <= value <= _MAX_HEALTH_EVENTS_PER_PROVIDER
            for value in counts
        )
        or rate_limit_count + server_error_count + timeout_count > sample_count
        or success_rate is None
        or not 0 <= success_rate <= 1
        or health_score is None
        or not 0 <= health_score <= 1
        or (p50_source is not None and p50 is None)
        or (p99_source is not None and p99 is None)
        or ((p50 is None) != (p99 is None))
        or (
            p50 is not None
            and (
                not 0 <= p50 <= _MAX_LATENCY_MS
                or p99 is None
                or not p50 <= p99 <= _MAX_LATENCY_MS
            )
        )
    ):
        raise ValueError("invalid health snapshot")
    return _HealthSnapshotState(
        provider_id=provider_id,
        window_seconds=window_seconds,
        sample_count=sample_count,
        success_rate=success_rate,
        p50_latency_ms=p50,
        p99_latency_ms=p99,
        rate_limit_count=rate_limit_count,
        server_error_count=server_error_count,
        timeout_count=timeout_count,
        health_score=health_score,
    )


def _validated_fallback_decision_state(
    decision: FallbackDecision,
) -> _FallbackDecisionState:
    try:
        action = decision.action
        candidate = decision.candidate
        error_code = decision.error_code
        retryable = decision.retryable
        if (
            type(action) is not FallbackAction
            or (candidate is not None and type(candidate) is not Candidate)
            or type(error_code) is not str
            or error_code not in _SANITIZED_ERROR_CODES
            or type(retryable) is not bool
        ):
            raise ValueError("invalid fallback result")
        if candidate is not None:
            _validate_candidate(candidate)
        if action in {
            FallbackAction.RETRY,
            FallbackAction.DEGRADE_TO_CHEAP,
        }:
            if candidate is None or not retryable:
                raise ValueError("invalid fallback result")
        elif candidate is not None or retryable:
            raise ValueError("invalid fallback result")
    except Exception:
        raise ValueError("invalid fallback result") from None
    return _FallbackDecisionState(action, candidate, error_code, retryable)


def _validate_candidate(candidate: Candidate) -> None:
    score = _valid_finite_number(candidate.health_score)
    if (
        _valid_provider_id(candidate.provider_id) is None
        or _valid_model(candidate.model) is None
        or _valid_cost(candidate.estimated_cost) is None
        or type(candidate.should_send) is not Decision
        or type(candidate.breaker_state) is not BreakerState
        or score is None
        or not 0 <= score <= 1
    ):
        raise ValueError("invalid fallback candidate")


def _bounded_candidates(candidates: Iterable[Candidate]) -> tuple[Candidate, ...]:
    if isinstance(candidates, (str, bytes, bytearray, dict)):
        raise ValueError("invalid fallback candidates")
    try:
        iterator = iter(candidates)
    except Exception:
        raise ValueError("invalid fallback candidates") from None
    result: list[Candidate] = []
    try:
        for item in iterator:
            if len(result) >= _MAX_CANDIDATES or type(item) is not Candidate:
                raise ValueError("invalid fallback candidates")
            _validate_candidate(item)
            if any(existing.provider_id == item.provider_id for existing in result):
                raise ValueError("invalid fallback candidates")
            result.append(item)
    except Exception:
        raise ValueError("invalid fallback candidates") from None
    return tuple(result)


def _validate_health_event(event: HealthEvent) -> None:
    observed_at = _valid_finite_number(event.observed_at)
    latency = (
        None if event.latency_ms is None else _valid_finite_number(event.latency_ms)
    )
    if (
        _valid_provider_id(event.provider_id) is None
        or type(event.status) is not HealthStatus
        or observed_at is None
        or (
            event.latency_ms is not None
            and (latency is None or not 0 <= latency <= _MAX_LATENCY_MS)
        )
    ):
        raise ValueError("invalid health event")


def _health_snapshot(
    provider_id: str,
    window_seconds: int,
    events: list[HealthEvent],
) -> HealthSnapshot:
    sample_count = len(events)
    success_count = sum(
        event.status is HealthStatus.SUCCESS for event in events
    )
    rate_limit_count = sum(
        event.status is HealthStatus.RATE_LIMIT for event in events
    )
    server_error_count = sum(
        event.status is HealthStatus.SERVER_ERROR for event in events
    )
    timeout_count = sum(
        event.status is HealthStatus.TIMEOUT for event in events
    )
    latencies = sorted(
        event.latency_ms
        for event in events
        if event.latency_ms is not None
    )
    success_rate = (
        round(success_count / sample_count, 6) if sample_count else 0.0
    )
    if sample_count:
        error_penalty = (
            rate_limit_count * 0.05
            + server_error_count * 0.15
            + timeout_count * 0.25
        ) / sample_count
        health_score = round(
            max(0.0, min(1.0, success_rate - error_penalty)),
            6,
        )
    else:
        # No samples is neutral rather than a fabricated healthy signal.
        health_score = 0.5
    return HealthSnapshot(
        provider_id=provider_id,
        window_seconds=window_seconds,
        sample_count=sample_count,
        success_rate=success_rate,
        p50_latency_ms=_percentile(latencies, 0.50),
        p99_latency_ms=_percentile(latencies, 0.99),
        rate_limit_count=rate_limit_count,
        server_error_count=server_error_count,
        timeout_count=timeout_count,
        health_score=health_score,
    )


def _percentile(values: list[float], proportion: float) -> float | None:
    if not values:
        return None
    index = max(0, math.ceil(len(values) * proportion) - 1)
    return values[index]


def _validate_replay_state(state: ReplayState) -> None:
    if any(
        type(value) is not bool
        for value in (
            state.output_delta_observed,
            state.tool_call_observed,
            state.provider_side_effect_observed,
            state.client_aborted,
        )
    ):
        raise ValueError("invalid replay state")


def _exhausted(action: FallbackAction) -> FallbackDecision:
    return FallbackDecision(action, None, "fallback_exhausted", False)


__all__ = [
    "BreakerState",
    "Candidate",
    "CircuitBreaker",
    "FailureSignal",
    "FallbackAction",
    "FallbackDecision",
    "FallbackSelector",
    "HealthEvent",
    "HealthSnapshot",
    "HealthStatus",
    "HealthWindow",
    "ReplayState",
]
