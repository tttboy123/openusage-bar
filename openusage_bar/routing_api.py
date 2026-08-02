"""Strict, content-free Route Decision API contracts and controller."""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from .query import QueryService
from .routing_contract import (
    DecisionContext,
    RejectedTarget,
    RouteConstraints,
    RouteDecision,
    RouteRequest,
    RouteSession,
    RouteTask,
    ScoredTarget,
)
from .routing_engine import decide_route
from .routing_facts import build_target_facts
from .routing_policy import RoutePolicy, built_in_policies, built_in_policy
from .routing_store import (
    DecisionEvidence,
    RoutingStore,
    StoredDecision,
    StoredRejectedTarget,
    StoredScoredTarget,
    try_record_decision,
)
from .routing_targets import RouteTargetConfiguration
from .runtime_store import RuntimeSummary


SCHEMA_VERSION = "1.0"
DEFAULT_DECISION_TTL = timedelta(seconds=30)
DEFAULT_RUNTIME_WINDOW = timedelta(minutes=15)
MAX_REQUEST_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 256 * 1024

_REQUEST_REF = re.compile(r"^req_[0-9a-f]{16,64}$")

_TOP_LEVEL_KEYS = frozenset(
    {"schemaVersion", "clientRequestRef", "policyId", "task", "constraints", "session"}
)
_TOP_LEVEL_REQUIRED = frozenset({"schemaVersion", "policyId", "task"})
_TASK_KEYS = frozenset(
    {
        "kind",
        "requiredCapabilities",
        "estimatedInputTokens",
        "maxOutputTokens",
        "minimumContextWindowTokens",
        "privacy",
        "regions",
    }
)
_CONSTRAINT_KEYS = frozenset(
    {
        "allowProviders",
        "denyProviders",
        "allowTargets",
        "denyTargets",
        "maximumEstimatedCostMicrounits",
        "costCurrency",
    }
)
_SESSION_KEYS = frozenset(
    {"sessionRef", "remainingBudgetMicrounits", "reserveMicrounits", "budgetCurrency"}
)


class RoutingAPIProblem(RuntimeError):
    """Stable sanitized error returned by the Decision API."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = dict(details or {})


@dataclass(frozen=True)
class RoutingRequestEnvelope:
    client_request_ref: str | None
    request: RouteRequest


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json_value(raw: str | bytes) -> object:
    if isinstance(raw, str):
        encoded = raw.encode("utf-8")
    elif isinstance(raw, bytes):
        encoded = raw
    else:
        raise ValueError("routing request is invalid")
    if not encoded or len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("routing request is invalid")
    try:
        text = encoded.decode("utf-8")
        decoder = json.JSONDecoder(object_pairs_hook=_strict_object)
        value, offset = decoder.raw_decode(text)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("routing request is invalid") from error
    if text[offset:].strip():
        raise ValueError("routing request is invalid")
    return value


def _exact_mapping(
    value: object,
    allowed: frozenset[str],
    required: frozenset[str] | None = None,
) -> dict[str, object]:
    if not isinstance(value, dict) or not set(value) <= allowed:
        raise ValueError("routing request is invalid")
    if required is not None and not required <= set(value):
        raise ValueError("routing request is invalid")
    return value


def _required_exact(value: object, keys: frozenset[str]) -> dict[str, object]:
    result = _exact_mapping(value, keys, keys)
    if set(result) != keys:
        raise ValueError("routing request is invalid")
    return result


def _id_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("routing request is invalid")
    return tuple(value)


def decode_decision_request(
    payload: object,
) -> RoutingRequestEnvelope:
    """Decode one exact metadata-only routing request.

    Strings and bytes are decoded with duplicate-key and trailing-value
    rejection. Mapping inputs are intended for already strict internal callers.
    """
    value = _json_value(payload) if isinstance(payload, (str, bytes)) else payload
    raw = _exact_mapping(value, _TOP_LEVEL_KEYS, _TOP_LEVEL_REQUIRED)
    if raw["schemaVersion"] != SCHEMA_VERSION:
        raise ValueError("routing request is invalid")
    client_ref = raw.get("clientRequestRef")
    if client_ref is not None and (
        not isinstance(client_ref, str) or _REQUEST_REF.fullmatch(client_ref) is None
    ):
        raise ValueError("routing request is invalid")

    task_raw = _required_exact(raw["task"], _TASK_KEYS)
    constraints_raw = (
        _required_exact(raw["constraints"], _CONSTRAINT_KEYS)
        if "constraints" in raw
        else {
            "allowProviders": [],
            "denyProviders": [],
            "allowTargets": [],
            "denyTargets": [],
            "maximumEstimatedCostMicrounits": None,
            "costCurrency": None,
        }
    )
    session_raw = None
    if raw.get("session") is not None:
        session_raw = _required_exact(raw["session"], _SESSION_KEYS)
    try:
        task = RouteTask(
            kind=task_raw["kind"],
            required_capabilities=_id_list(task_raw["requiredCapabilities"]),
            estimated_input_tokens=task_raw["estimatedInputTokens"],
            max_output_tokens=task_raw["maxOutputTokens"],
            minimum_context_window_tokens=task_raw["minimumContextWindowTokens"],
            privacy=task_raw["privacy"],
            regions=_id_list(task_raw["regions"]),
        )
        constraints = RouteConstraints(
            allow_providers=_id_list(constraints_raw["allowProviders"]),
            deny_providers=_id_list(constraints_raw["denyProviders"]),
            allow_targets=_id_list(constraints_raw["allowTargets"]),
            deny_targets=_id_list(constraints_raw["denyTargets"]),
            maximum_estimated_cost_micros=constraints_raw[
                "maximumEstimatedCostMicrounits"
            ],
            cost_currency=constraints_raw["costCurrency"],
        )
        session = None
        if session_raw is not None:
            session = RouteSession(
                session_ref=session_raw["sessionRef"],
                remaining_budget_micros=session_raw["remainingBudgetMicrounits"],
                reserve_micros=session_raw["reserveMicrounits"],
                budget_currency=session_raw["budgetCurrency"],
            )
        request = RouteRequest(
            policy_id=raw["policyId"],
            task=task,
            constraints=constraints,
            session=session,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("routing request is invalid") from error
    return RoutingRequestEnvelope(client_ref, request)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("routing clock is invalid")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _components_wire(value) -> dict[str, int]:
    return {
        "reliability": value.reliability,
        "headroom": value.headroom,
        "latency": value.latency,
        "cost": value.cost,
    }


def _scored_wire(value: ScoredTarget) -> dict[str, object]:
    return {
        "targetId": value.target_id,
        "providerId": value.provider_id,
        "accountRef": value.account_ref,
        "modelId": value.model_id,
        "score": value.score,
        "components": _components_wire(value.components),
        "reasons": list(value.reasons),
    }


def _rejected_wire(value: RejectedTarget) -> dict[str, object]:
    return {"targetId": value.target_id, "reasonCodes": list(value.reason_codes)}


def _decision_wire(
    decision_id: str,
    decision: RouteDecision,
    *,
    evidence_stored: bool,
    simulated: bool,
) -> dict[str, object]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "decisionId": decision_id,
        "generatedAt": decision.generated_at,
        "expiresAt": decision.expires_at,
        "policy": {
            "policyId": decision.policy_id,
            "policyRevision": decision.policy_revision,
        },
        "facts": {
            "dataRevision": decision.data_revision,
            "runtimeRevision": decision.runtime_revision,
        },
        "selected": None if decision.selected is None else _scored_wire(decision.selected),
        "alternatives": [_scored_wire(value) for value in decision.alternatives],
        "rejected": [_rejected_wire(value) for value in decision.rejected],
        "warnings": [],
        "evidenceStored": evidence_stored,
        "simulated": simulated,
    }


def _stored_scored_wire(value: StoredScoredTarget) -> dict[str, object]:
    return {
        "targetId": value.target_id,
        "score": value.score,
        "components": _components_wire(value.components),
        "reasons": list(value.reasons),
    }


def _stored_rejected_wire(value: StoredRejectedTarget) -> dict[str, object]:
    return {"targetId": value.target_id, "reasonCodes": list(value.reason_codes)}


def stored_decision_wire(value: StoredDecision) -> dict[str, object]:
    return {
        "decisionId": value.decision_id,
        "generatedAt": value.created_at,
        "expiresAt": value.expires_at,
        "policy": {
            "policyId": value.policy_id,
            "policyRevision": value.policy_revision,
        },
        "facts": {
            "dataRevision": value.data_revision,
            "runtimeRevision": value.runtime_revision,
        },
        "clientRequestRef": value.client_request_ref,
        "sessionRef": value.session_ref,
        "selected": (
            None if value.selected is None else _stored_scored_wire(value.selected)
        ),
        "alternatives": [
            _stored_scored_wire(item) for item in value.alternatives
        ],
        "rejected": [
            _stored_rejected_wire(item) for item in value.rejected
        ],
    }


def _policy_wire(value: RoutePolicy) -> dict[str, object]:
    return {
        "policyId": value.policy_id,
        "policyRevision": value.revision,
        "weights": {
            "reliability": value.reliability_weight,
            "headroom": value.headroom_weight,
            "latency": value.latency_weight,
            "cost": value.cost_weight,
        },
        "requirements": {
            "minimumHeadroomBasisPoints": value.min_headroom_bp,
            "maximumErrorRateBasisPoints": value.max_error_rate_bp,
            "minimumRuntimeSamples": value.min_runtime_samples,
            "requireCost": value.require_cost,
            "requireRuntime": value.require_runtime,
        },
    }


class RoutingController:
    """Fact reader plus pure engine; it never owns secrets or execution."""

    def __init__(
        self,
        *,
        query: QueryService,
        target_loader: Callable[[], RouteTargetConfiguration],
        evidence_store: RoutingStore,
        runtime_reader: Callable[[datetime, datetime], RuntimeSummary | None],
        available_connections: Callable[[], tuple[str, ...]],
        clock: Callable[[], datetime] | None = None,
        decision_ttl: timedelta = DEFAULT_DECISION_TTL,
        runtime_window: timedelta = DEFAULT_RUNTIME_WINDOW,
    ) -> None:
        if decision_ttl <= timedelta(0) or decision_ttl > timedelta(minutes=5):
            raise ValueError("routing decision TTL is invalid")
        if runtime_window <= timedelta(0) or runtime_window > timedelta(hours=24):
            raise ValueError("routing Runtime window is invalid")
        self.query = query
        self.target_loader = target_loader
        self.evidence_store = evidence_store
        self.runtime_reader = runtime_reader
        self.available_connections = available_connections
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.decision_ttl = decision_ttl
        self.runtime_window = runtime_window

    def decide(self, payload: object, *, simulated: bool) -> dict[str, object]:
        try:
            envelope = decode_decision_request(payload)
        except ValueError as error:
            raise RoutingAPIProblem(400, "invalid_request", "Invalid routing request.") from error
        try:
            policy = built_in_policy(envelope.request.policy_id)
        except ValueError as error:
            raise RoutingAPIProblem(404, "policy_not_found", "Routing policy was not found.") from error
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise RoutingAPIProblem(500, "internal_error", "Routing is unavailable.")
        now = now.astimezone(timezone.utc)
        try:
            configuration = self.target_loader()
            snapshot = self.query.resource_snapshot(now.astimezone().date())
        except Exception as error:
            raise RoutingAPIProblem(503, "facts_unavailable", "Routing facts are unavailable.") from error
        try:
            runtime = self.runtime_reader(now - self.runtime_window, now)
        except Exception:
            runtime = None
        if policy.require_runtime and runtime is None:
            raise RoutingAPIProblem(
                503, "runtime_unavailable", "Runtime routing facts are unavailable."
            )
        try:
            facts = build_target_facts(
                targets=configuration.targets,
                route_request=envelope.request,
                resource_snapshot=snapshot,
                runtime_summary=runtime,
                available_connections=self.available_connections(),
            )
            context = DecisionContext(
                generated_at=_timestamp(now),
                expires_at=_timestamp(now + self.decision_ttl),
                data_revision=snapshot.data_revision,
                runtime_revision=(None if runtime is None else runtime.runtime_revision),
            )
            decision = decide_route(
                envelope.request,
                configuration.targets,
                facts,
                policy,
                context,
            )
        except RoutingAPIProblem:
            raise
        except Exception as error:
            raise RoutingAPIProblem(500, "internal_error", "Routing is unavailable.") from error
        decision_id = "route_" + secrets.token_hex(16)
        evidence_stored = False
        if not simulated:
            evidence_stored = try_record_decision(
                self.evidence_store,
                DecisionEvidence(
                    decision_id,
                    envelope.client_request_ref,
                    None if envelope.request.session is None else envelope.request.session.session_ref,
                    decision,
                ),
            )
        representation = _decision_wire(
            decision_id,
            decision,
            evidence_stored=evidence_stored,
            simulated=simulated,
        )
        if decision.selected is None:
            raise RoutingAPIProblem(
                409,
                "no_route",
                "No eligible route is available.",
                details={
                    "decisionId": decision_id,
                    "generatedAt": decision.generated_at,
                    "expiresAt": decision.expires_at,
                    "policy": representation["policy"],
                    "facts": representation["facts"],
                    "rejected": representation["rejected"],
                    "evidenceStored": evidence_stored,
                    "simulated": simulated,
                },
            )
        return representation

    def policies(self) -> dict[str, object]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "policies": [_policy_wire(value) for value in built_in_policies()],
        }

    def list_decisions(
        self, *, before: str | None, limit: int
    ) -> dict[str, object]:
        try:
            values = self.evidence_store.list_decisions(before=before, limit=limit)
            revision = self.evidence_store.revision()
        except (TypeError, ValueError) as error:
            raise RoutingAPIProblem(400, "invalid_request", "Invalid routing history query.") from error
        except Exception as error:
            raise RoutingAPIProblem(503, "facts_unavailable", "Routing history is unavailable.") from error
        return {
            "schemaVersion": SCHEMA_VERSION,
            "routingRevision": revision,
            "decisions": [stored_decision_wire(value) for value in values],
            "nextBefore": values[-1].decision_id if len(values) == limit else None,
        }

    def get_decision(self, decision_id: str) -> dict[str, object]:
        try:
            value = self.evidence_store.get_decision(decision_id)
        except (TypeError, ValueError) as error:
            raise RoutingAPIProblem(400, "invalid_request", "Invalid decision identifier.") from error
        except Exception as error:
            raise RoutingAPIProblem(503, "facts_unavailable", "Routing history is unavailable.") from error
        if value is None:
            raise RoutingAPIProblem(404, "not_found", "Route decision was not found.")
        return {"schemaVersion": SCHEMA_VERSION, "decision": stored_decision_wire(value)}
