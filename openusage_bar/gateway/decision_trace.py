"""Bounded, process-lifetime public Decision Trace projections.

Decision traces are deliberately ephemeral: they exist only in the active
Gateway process and are discarded when that process exits.  The recorder never
accepts prompts, responses, models, endpoints, credentials, raw errors, or
private account identifiers.  Every trace is rebuilt from a closed public
contract both when it is recorded and when it is read.
"""

from __future__ import annotations

import copy
import re
import secrets
import threading
from datetime import datetime, timezone
from typing import Callable


DECISION_TRACE_API_VERSION = "gateway-decision-trace.openusage/v1"
MAX_DECISION_TRACES = 128
MAX_TRACE_EXCLUSIONS = 64
MAX_FACTS_WINDOW_SECONDS = 31 * 24 * 60 * 60

_TRACE_FIELDS = frozenset(
    {
        "traceId",
        "occurredAt",
        "kind",
        "execution",
        "outcome",
        "reason",
        "pool",
        "selected",
        "exclusions",
        "fallback",
        "factsWindow",
    }
)
_TRACE_ID = re.compile(r"trace_[0-9a-f]{32}\Z")
_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z"
)
_FACTS_WINDOW = re.compile(r"([1-9][0-9]{0,6})([smhd])\Z")
_STABLE_ID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_ACCOUNT_DISPLAY_ID = re.compile(r"acct_[0-9a-f]{12}\Z")
_PROVIDER_IDS = frozenset(
    {"anthropic", "deepseek", "ollama", "openai", "openrouter"}
)
_KINDS = frozenset(
    {"route_advice", "gateway_execution", "pool_selection"}
)
_EXECUTIONS = frozenset({"advice_only", "executed"})
_OUTCOMES = frozenset(
    {"yes", "no", "defer", "selected", "unavailable", "succeeded", "failed"}
)
_REASONS = frozenset(
    {
        "approaching_limit",
        "burn_rate_too_high",
        "quota_healthy",
        "quota_low",
        "quota_unknown",
    }
)
_POOL_STRATEGIES = frozenset(
    {
        "fixed-first",
        "round-robin",
        "sticky",
        "quota-aware",
        "cost",
        "latency",
        "reliability",
    }
)
_EXCLUSION_REASONS = frozenset(
    {
        "cooldown",
        "credential_backend_unavailable",
        "cross_model_unconfirmed",
        "cross_provider_unconfirmed",
        "cross_region_unconfirmed",
        "disabled",
        "health_unknown",
        "metric_unknown",
        "quota_unknown",
        "unhealthy",
    }
)
_FALLBACK_ACTIONS = frozenset(
    {"none", "retry", "fail", "degrade_to_cheap"}
)
_MAX_SAFE_INTEGER = 2**53 - 1


def facts_window_duration_seconds(value: object) -> int | None:
    """Derive a bounded duration without retaining the caller's window text."""

    if type(value) is not str:
        return None
    match = _FACTS_WINDOW.fullmatch(value)
    if match is None:
        return None
    multiplier = {"s": 1, "m": 60, "h": 3_600, "d": 86_400}[match.group(2)]
    duration = int(match.group(1)) * multiplier
    return duration if 1 <= duration <= MAX_FACTS_WINDOW_SECONDS else None


def _canonical_utc(value: object) -> str | None:
    if type(value) is not datetime or value.tzinfo is None:
        return None
    try:
        offset = value.utcoffset()
        if offset is None:
            return None
        normalized = value.astimezone(timezone.utc)
        return normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except (OverflowError, ValueError):
        return None


def _valid_utc(value: object) -> str | None:
    if type(value) is not str or _UTC_TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return None
    canonical = parsed.replace(tzinfo=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    return value if value == canonical else None


def _validated_pool(value: object) -> dict[str, object] | None:
    if type(value) is not dict or set(value) != {"poolId", "revision", "strategy"}:
        return None
    pool_id = value.get("poolId")
    revision = value.get("revision")
    strategy = value.get("strategy")
    if (
        type(pool_id) is not str
        or _STABLE_ID.fullmatch(pool_id) is None
        or type(revision) is not int
        or not 1 <= revision <= _MAX_SAFE_INTEGER
        or type(strategy) is not str
        or strategy not in _POOL_STRATEGIES
    ):
        return None
    return {"poolId": pool_id, "revision": revision, "strategy": strategy}


def _validated_selected(value: object) -> dict[str, object] | None:
    if type(value) is not dict or set(value) != {"providerId", "accountDisplayId"}:
        return None
    provider_id = value.get("providerId")
    display_id = value.get("accountDisplayId")
    if provider_id is not None and (
        type(provider_id) is not str or provider_id not in _PROVIDER_IDS
    ):
        return None
    if display_id is not None and (
        type(display_id) is not str
        or _ACCOUNT_DISPLAY_ID.fullmatch(display_id) is None
    ):
        return None
    return {"providerId": provider_id, "accountDisplayId": display_id}


def _validated_exclusions(value: object) -> list[dict[str, str]] | None:
    if type(value) is not list or len(value) > MAX_TRACE_EXCLUSIONS:
        return None
    result: list[dict[str, str]] = []
    for item in value:
        if type(item) is not dict or set(item) != {"accountDisplayId", "reason"}:
            return None
        display_id = item.get("accountDisplayId")
        reason = item.get("reason")
        if (
            type(display_id) is not str
            or _ACCOUNT_DISPLAY_ID.fullmatch(display_id) is None
            or type(reason) is not str
            or reason not in _EXCLUSION_REASONS
        ):
            return None
        result.append({"accountDisplayId": display_id, "reason": reason})
    return result


def _validated_fallback(value: object) -> dict[str, object] | None:
    if type(value) is not dict or set(value) != {
        "attempted",
        "attemptCount",
        "finalAction",
    }:
        return None
    attempted = value.get("attempted")
    count = value.get("attemptCount")
    action = value.get("finalAction")
    if (
        type(attempted) is not bool
        or type(count) is not int
        or not 0 <= count <= 2
        or type(action) is not str
        or action not in _FALLBACK_ACTIONS
    ):
        return None
    if not attempted and (count > 1 or action != "none"):
        return None
    if attempted and (count < 1 or action == "none"):
        return None
    if action in {"retry", "degrade_to_cheap"} and count != 2:
        return None
    return {"attempted": attempted, "attemptCount": count, "finalAction": action}


def _validated_facts_window(value: object) -> dict[str, int] | None:
    if type(value) is not dict or set(value) != {"durationSeconds"}:
        return None
    duration = value.get("durationSeconds")
    if (
        type(duration) is not int
        or not 1 <= duration <= MAX_FACTS_WINDOW_SECONDS
    ):
        return None
    return {"durationSeconds": duration}


def validate_decision_trace(value: object) -> dict[str, object] | None:
    """Return an exact detached public projection, or fail closed with ``None``."""

    try:
        if type(value) is not dict or set(value) != _TRACE_FIELDS:
            return None
        trace_id = value.get("traceId")
        occurred_at = _valid_utc(value.get("occurredAt"))
        kind = value.get("kind")
        execution = value.get("execution")
        outcome = value.get("outcome")
        reason = value.get("reason")
        if (
            type(trace_id) is not str
            or _TRACE_ID.fullmatch(trace_id) is None
            or occurred_at is None
            or type(kind) is not str
            or kind not in _KINDS
            or type(execution) is not str
            or execution not in _EXECUTIONS
            or type(outcome) is not str
            or outcome not in _OUTCOMES
            or (
                reason is not None
                and (type(reason) is not str or reason not in _REASONS)
            )
        ):
            return None

        pool = None if value.get("pool") is None else _validated_pool(value.get("pool"))
        selected = (
            None
            if value.get("selected") is None
            else _validated_selected(value.get("selected"))
        )
        exclusions = _validated_exclusions(value.get("exclusions"))
        fallback = (
            None
            if value.get("fallback") is None
            else _validated_fallback(value.get("fallback"))
        )
        facts_window = (
            None
            if value.get("factsWindow") is None
            else _validated_facts_window(value.get("factsWindow"))
        )
        if (
            (value.get("pool") is not None and pool is None)
            or (value.get("selected") is not None and selected is None)
            or exclusions is None
            or (value.get("fallback") is not None and fallback is None)
            or (value.get("factsWindow") is not None and facts_window is None)
        ):
            return None

        if kind == "route_advice":
            if (
                execution != "advice_only"
                or outcome not in {"yes", "no", "defer"}
                or reason is None
                or pool is not None
                or selected is not None
                or exclusions
                or fallback is not None
            ):
                return None
        elif kind == "gateway_execution":
            if (
                execution != "executed"
                or outcome not in {"succeeded", "failed"}
                or reason is not None
                or pool is not None
                or selected is None
                or selected["providerId"] not in _PROVIDER_IDS
                or selected["accountDisplayId"] is not None
                or exclusions
                or fallback is None
                or facts_window is not None
            ):
                return None
        else:
            if (
                execution != "executed"
                or outcome not in {"selected", "unavailable"}
                or reason is not None
                or pool is None
                or fallback is not None
                or facts_window is not None
            ):
                return None
            if outcome == "selected":
                if (
                    selected is None
                    or selected["providerId"] not in _PROVIDER_IDS
                    or selected["accountDisplayId"] is None
                ):
                    return None
            elif selected is not None:
                return None

        return {
            "traceId": trace_id,
            "occurredAt": occurred_at,
            "kind": kind,
            "execution": execution,
            "outcome": outcome,
            "reason": reason,
            "pool": pool,
            "selected": selected,
            "exclusions": exclusions,
            "fallback": fallback,
            "factsWindow": facts_window,
        }
    except Exception:
        return None


def validate_decision_traces_payload(value: object) -> dict[str, object] | None:
    """Validate the exact newest-first trace collection root."""

    try:
        if (
            type(value) is not dict
            or set(value) != {"apiVersion", "traces"}
            or value.get("apiVersion") != DECISION_TRACE_API_VERSION
            or type(value.get("traces")) is not list
            or len(value["traces"]) > MAX_DECISION_TRACES
        ):
            return None
        traces: list[dict[str, object]] = []
        trace_ids: set[str] = set()
        previous_time: str | None = None
        for item in value["traces"]:
            trace = validate_decision_trace(item)
            if trace is None or trace["traceId"] in trace_ids:
                return None
            occurred_at = trace["occurredAt"]
            assert type(occurred_at) is str
            if previous_time is not None and occurred_at > previous_time:
                return None
            trace_ids.add(trace["traceId"])
            previous_time = occurred_at
            traces.append(trace)
        return {"apiVersion": DECISION_TRACE_API_VERSION, "traces": traces}
    except Exception:
        return None


class DecisionTraceRecorder:
    """Thread-safe, bounded trace memory for one Gateway process lifetime."""

    def __init__(
        self,
        *,
        max_traces: int = MAX_DECISION_TRACES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(max_traces) is not int or not 1 <= max_traces <= MAX_DECISION_TRACES:
            raise ValueError("invalid Decision Trace capacity")
        if clock is not None and not callable(clock):
            raise ValueError("invalid Decision Trace clock")
        self._max_traces = max_traces
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._traces: list[dict[str, object]] = []

    def record_route_advice(
        self,
        *,
        outcome: object,
        reason: object,
        facts_window_duration_seconds: object,
        occurred_at: datetime | None = None,
    ) -> bool:
        facts_window = (
            None
            if facts_window_duration_seconds is None
            else {"durationSeconds": facts_window_duration_seconds}
        )
        return self._record(
            {
                "kind": "route_advice",
                "execution": "advice_only",
                "outcome": outcome,
                "reason": reason,
                "pool": None,
                "selected": None,
                "exclusions": [],
                "fallback": None,
                "factsWindow": facts_window,
            },
            occurred_at=occurred_at,
        )

    def record_gateway_execution(
        self,
        *,
        provider_id: object,
        outcome: object,
        fallback: object,
        occurred_at: datetime | None = None,
    ) -> bool:
        return self._record(
            {
                "kind": "gateway_execution",
                "execution": "executed",
                "outcome": outcome,
                "reason": None,
                "pool": None,
                "selected": {
                    "providerId": provider_id,
                    "accountDisplayId": None,
                },
                "exclusions": [],
                "fallback": fallback,
                "factsWindow": None,
            },
            occurred_at=occurred_at,
        )

    def record_pool_selection(
        self,
        *,
        pool_id: object,
        revision: object,
        strategy: object,
        selected_provider_id: object,
        selected_account_display_id: object,
        exclusions: object,
        occurred_at: datetime | None = None,
    ) -> bool:
        selected = None
        outcome = "unavailable"
        if selected_provider_id is not None or selected_account_display_id is not None:
            selected = {
                "providerId": selected_provider_id,
                "accountDisplayId": selected_account_display_id,
            }
            outcome = "selected"
        return self._record(
            {
                "kind": "pool_selection",
                "execution": "executed",
                "outcome": outcome,
                "reason": None,
                "pool": {
                    "poolId": pool_id,
                    "revision": revision,
                    "strategy": strategy,
                },
                "selected": selected,
                "exclusions": exclusions,
                "fallback": None,
                "factsWindow": None,
            },
            occurred_at=occurred_at,
        )

    def snapshot(self) -> dict[str, object]:
        try:
            with self._lock:
                candidate = {
                    "apiVersion": DECISION_TRACE_API_VERSION,
                    "traces": copy.deepcopy(self._traces),
                }
            validated = validate_decision_traces_payload(candidate)
        except Exception:
            validated = None
        return validated if validated is not None else {
            "apiVersion": DECISION_TRACE_API_VERSION,
            "traces": [],
        }

    def _record(
        self,
        fields: dict[str, object],
        *,
        occurred_at: datetime | None,
    ) -> bool:
        try:
            active_time = occurred_at if occurred_at is not None else self._clock()
            timestamp = _canonical_utc(active_time)
            if timestamp is None:
                return False
            with self._lock:
                existing_ids = {item["traceId"] for item in self._traces}
                trace_id = None
                for _attempt in range(4):
                    candidate_id = f"trace_{secrets.token_hex(16)}"
                    if candidate_id not in existing_ids:
                        trace_id = candidate_id
                        break
                if trace_id is None:
                    return False
                candidate = {
                    "traceId": trace_id,
                    "occurredAt": timestamp,
                    **fields,
                }
                trace = validate_decision_trace(candidate)
                if trace is None:
                    return False
                self._traces.append(trace)
                self._traces.sort(
                    key=lambda item: (item["occurredAt"], item["traceId"]),
                    reverse=True,
                )
                del self._traces[self._max_traces :]
            return True
        except Exception:
            return False


__all__ = [
    "DECISION_TRACE_API_VERSION",
    "DecisionTraceRecorder",
    "facts_window_duration_seconds",
    "MAX_DECISION_TRACES",
    "MAX_TRACE_EXCLUSIONS",
    "validate_decision_trace",
    "validate_decision_traces_payload",
]
