"""Strict, content-free Route Decision API contracts and controller."""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import socketserver
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import parse_qsl, urlsplit

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
from .routing_policy import RoutePolicy, built_in_policies
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
from .local_api import (
    DEFAULT_CLIENT_TIMEOUT,
    DEFAULT_MAX_THREADS,
    DEFAULT_REQUEST_DEADLINE,
    MAX_REQUEST_LINE,
    _BoundedThreads,
    _peer_is_current_user,
    _prepare_socket_path,
    _unlink_socket_if,
)


SCHEMA_VERSION = "1.0"
DEFAULT_DECISION_TTL = timedelta(seconds=30)
DEFAULT_RUNTIME_WINDOW = timedelta(minutes=15)
MAX_REQUEST_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_QUERY_BYTES = 4 * 1024

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

ROUTING_API_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://openusage.bar/schemas/routing-api-v1.schema.json",
    "title": "OpenUsage Bar Route Decision API",
    "type": "object",
    "additionalProperties": False,
    "required": ["schemaVersion", "policyId", "task"],
    "properties": {
        "schemaVersion": {"const": SCHEMA_VERSION},
        "clientRequestRef": {
            "type": "string",
            "pattern": r"^req_[0-9a-f]{16,64}$",
        },
        "policyId": {
            "type": "string",
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
        },
        "task": {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(_TASK_KEYS),
            "properties": {
                "kind": {"enum": ["audio", "chat", "code", "embedding", "image", "other", "reasoning"]},
                "requiredCapabilities": {"$ref": "#/$defs/idList"},
                "estimatedInputTokens": {"$ref": "#/$defs/counter"},
                "maxOutputTokens": {"$ref": "#/$defs/counter"},
                "minimumContextWindowTokens": {"$ref": "#/$defs/counter"},
                "privacy": {"enum": ["allow_proxy", "direct_provider", "local_only"]},
                "regions": {"$ref": "#/$defs/idList"},
            },
        },
        "constraints": {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(_CONSTRAINT_KEYS),
            "properties": {
                "allowProviders": {"$ref": "#/$defs/idList"},
                "denyProviders": {"$ref": "#/$defs/idList"},
                "allowTargets": {"$ref": "#/$defs/idList"},
                "denyTargets": {"$ref": "#/$defs/idList"},
                "maximumEstimatedCostMicrounits": {
                    "anyOf": [{"$ref": "#/$defs/counter"}, {"type": "null"}]
                },
                "costCurrency": {
                    "anyOf": [{"$ref": "#/$defs/currency"}, {"type": "null"}]
                },
            },
        },
        "session": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": sorted(_SESSION_KEYS),
                    "properties": {
                        "sessionRef": {
                            "type": "string",
                            "pattern": r"^anon_[0-9a-f]{16,64}$",
                        },
                        "remainingBudgetMicrounits": {
                            "anyOf": [{"$ref": "#/$defs/counter"}, {"type": "null"}]
                        },
                        "reserveMicrounits": {
                            "anyOf": [{"$ref": "#/$defs/counter"}, {"type": "null"}]
                        },
                        "budgetCurrency": {
                            "anyOf": [{"$ref": "#/$defs/currency"}, {"type": "null"}]
                        },
                    },
                },
            ]
        },
    },
    "$defs": {
        "counter": {"type": "integer", "minimum": 0, "maximum": 1_000_000_000_000},
        "currency": {"type": "string", "pattern": r"^[A-Z][A-Z0-9_]{2,7}$"},
        "id": {
            "type": "string",
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
        },
        "idList": {
            "type": "array",
            "maxItems": 50,
            "uniqueItems": True,
            "items": {"$ref": "#/$defs/id"},
        },
    },
}


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


def _future_timestamp(value: object, now: datetime) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    parsed = parsed.astimezone(timezone.utc)
    return parsed if parsed > now else None


def _decision_expiry(now: datetime, ttl: timedelta, snapshot) -> datetime:
    candidates = [now + ttl]
    for source in snapshot.sources:
        boundary = _future_timestamp(source.stale_at, now)
        if boundary is not None:
            candidates.append(boundary)
    for quota in snapshot.quota_windows:
        boundary = _future_timestamp(quota.resets_at, now)
        if boundary is not None:
            candidates.append(boundary)
    return min(candidates)


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
        policy_loader: Callable[[], tuple[RoutePolicy, ...]] | None = None,
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
        self.policy_loader = policy_loader or (lambda: ())
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.decision_ttl = decision_ttl
        self.runtime_window = runtime_window

    def decide(self, payload: object, *, simulated: bool) -> dict[str, object]:
        try:
            envelope = decode_decision_request(payload)
        except ValueError as error:
            raise RoutingAPIProblem(400, "invalid_request", "Invalid routing request.") from error
        try:
            policies = {
                value.policy_id: value for value in self._policies()
            }
            policy = policies[envelope.request.policy_id]
        except KeyError as error:
            raise RoutingAPIProblem(404, "policy_not_found", "Routing policy was not found.") from error
        except Exception as error:
            raise RoutingAPIProblem(
                503, "facts_unavailable", "Routing policies are unavailable."
            ) from error
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
                expires_at=_timestamp(
                    _decision_expiry(now, self.decision_ttl, snapshot)
                ),
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
        try:
            policies = self._policies()
        except Exception as error:
            raise RoutingAPIProblem(
                503, "facts_unavailable", "Routing policies are unavailable."
            ) from error
        return {
            "schemaVersion": SCHEMA_VERSION,
            "policies": [_policy_wire(value) for value in policies],
        }

    def _policies(self) -> tuple[RoutePolicy, ...]:
        builtins = built_in_policies()
        custom = tuple(self.policy_loader())
        if any(not isinstance(value, RoutePolicy) for value in custom):
            raise ValueError("routing policies are invalid")
        ids = [value.policy_id for value in (*builtins, *custom)]
        if len(ids) != len(set(ids)):
            raise ValueError("routing policies are invalid")
        return tuple(sorted((*builtins, *custom), key=lambda value: value.policy_id))

    def health(self) -> dict[str, object]:
        try:
            configuration = self.target_loader()
            revision = self.evidence_store.revision()
        except Exception as error:
            raise RoutingAPIProblem(503, "facts_unavailable", "Routing is unavailable.") from error
        return {
            "schemaVersion": SCHEMA_VERSION,
            "health": {"ok": True, "status": "ok"},
            "targetRevision": configuration.revision,
            "targetCount": len(configuration.targets),
            "routingRevision": revision,
        }

    def schema(self) -> dict[str, object]:
        return {"schemaVersion": SCHEMA_VERSION, "schema": ROUTING_API_SCHEMA}

    def targets(self) -> dict[str, object]:
        try:
            configuration = self.target_loader()
        except Exception as error:
            raise RoutingAPIProblem(503, "facts_unavailable", "Route targets are unavailable.") from error
        return {
            "schemaVersion": SCHEMA_VERSION,
            "targetRevision": configuration.revision,
            "targets": [
                {
                    "targetId": value.target_id,
                    "providerId": value.provider_id,
                    "accountRef": value.account_ref,
                    "modelId": value.model_id,
                    "connectionRef": value.connection_ref,
                    "executionClass": value.execution_class,
                    "executionAdapterId": value.execution_adapter_id,
                    "resourceMode": value.resource_mode,
                    "factAccountRef": value.fact_account_ref,
                    "runtimeScopeRef": value.runtime_scope_ref,
                    "balanceCurrency": value.balance_currency,
                    "costCurrency": value.cost_currency,
                    "inputCostMicrosPerMillion": value.input_cost_micros_per_million,
                    "outputCostMicrosPerMillion": value.output_cost_micros_per_million,
                    "enabled": value.enabled,
                    "adapterAvailable": value.adapter_available,
                    "regions": list(value.regions),
                    "privacyClass": value.privacy_class,
                    "capabilities": list(value.capabilities),
                    "contextWindowTokens": value.context_window_tokens,
                    "qualityTier": value.quality_tier,
                }
                for value in configuration.targets
            ],
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


def _compact(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise RoutingAPIProblem(500, "internal_error", "Routing is unavailable.") from error
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise RoutingAPIProblem(500, "internal_error", "Routing is unavailable.")
    return encoded


def _problem_wire(value: RoutingAPIProblem) -> dict[str, object]:
    error: dict[str, object] = {"code": value.code, "message": value.message}
    if value.details:
        error["details"] = value.details
    return {"error": error}


def _query_parameters(raw_query: str) -> dict[str, str]:
    if len(raw_query.encode("utf-8")) > MAX_QUERY_BYTES:
        raise RoutingAPIProblem(413, "request_too_large", "Request is too large.")
    try:
        pairs = parse_qsl(
            raw_query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=3,
        ) if raw_query else []
    except ValueError as error:
        raise RoutingAPIProblem(400, "invalid_request", "Invalid routing request.") from error
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            raise RoutingAPIProblem(400, "invalid_request", "Invalid routing request.")
        values[key] = value
    return values


class RoutingAPIRouter:
    def __init__(self, controller: RoutingController) -> None:
        self.controller = controller

    def dispatch(self, method: str, target: str, body: bytes | None) -> tuple[int, bytes]:
        try:
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                raise RoutingAPIProblem(400, "invalid_request", "Invalid routing request.")
            parameters = _query_parameters(parsed.query)
            if method == "GET":
                if parsed.path == "/v1/health" and not parameters:
                    value = self.controller.health()
                elif parsed.path == "/v1/schema.json" and not parameters:
                    value = self.controller.schema()
                elif parsed.path == "/v1/policies" and not parameters:
                    value = self.controller.policies()
                elif parsed.path == "/v1/targets" and not parameters:
                    value = self.controller.targets()
                elif parsed.path == "/v1/decisions":
                    if not set(parameters) <= {"before", "limit"}:
                        raise RoutingAPIProblem(400, "invalid_request", "Invalid routing request.")
                    limit_raw = parameters.get("limit", "50")
                    try:
                        limit = int(limit_raw)
                    except (TypeError, ValueError) as error:
                        raise RoutingAPIProblem(400, "invalid_request", "Invalid routing request.") from error
                    if str(limit) != limit_raw or not 1 <= limit <= 100:
                        raise RoutingAPIProblem(400, "invalid_request", "Invalid routing request.")
                    value = self.controller.list_decisions(
                        before=parameters.get("before"), limit=limit
                    )
                elif parsed.path.startswith("/v1/decisions/") and not parameters:
                    identifier = parsed.path.removeprefix("/v1/decisions/")
                    if not identifier or "/" in identifier:
                        raise RoutingAPIProblem(404, "not_found", "Route was not found.")
                    value = self.controller.get_decision(identifier)
                else:
                    raise RoutingAPIProblem(404, "not_found", "Route was not found.")
            elif method == "POST":
                if parameters or body is None:
                    raise RoutingAPIProblem(400, "invalid_request", "Invalid routing request.")
                if parsed.path == "/v1/decisions":
                    value = self.controller.decide(body, simulated=False)
                elif parsed.path == "/v1/simulations":
                    value = self.controller.decide(body, simulated=True)
                else:
                    raise RoutingAPIProblem(404, "not_found", "Route was not found.")
            else:
                raise RoutingAPIProblem(405, "method_not_allowed", "Method is not allowed.")
            return 200, _compact(value)
        except RoutingAPIProblem as problem:
            return problem.status, _compact(_problem_wire(problem))
        except Exception:
            problem = RoutingAPIProblem(500, "internal_error", "Routing is unavailable.")
            return problem.status, _compact(_problem_wire(problem))


class RoutingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OpenUsageRouter/1"
    sys_version = ""

    def handle_one_request(self) -> None:
        try:
            self.raw_requestline = self.rfile.readline(MAX_REQUEST_LINE + 1)
            if len(self.raw_requestline) > MAX_REQUEST_LINE:
                self.requestline = ""
                self.request_version = "HTTP/1.1"
                self.command = ""
                self._problem(413, "request_too_large", "Request is too large.")
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            if self.request_version != "HTTP/1.1":
                self.request_version = "HTTP/1.1"
                self._problem(400, "invalid_request", "HTTP/1.1 is required.")
                return
            header_bytes = sum(
                len(name.encode("utf-8")) + len(value.encode("utf-8")) + 4
                for name, value in self.headers.raw_items()
            )
            if header_bytes > MAX_HEADER_BYTES:
                self._problem(413, "request_too_large", "Request is too large.")
                return
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1 or hosts[0] != "localhost":
                self._problem(400, "invalid_request", "Invalid routing request.")
                return
            method = getattr(self, "do_" + self.command, None)
            if method is None:
                self._problem(405, "method_not_allowed", "Method is not allowed.")
                return
            method()
            self.wfile.flush()
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    def do_GET(self) -> None:
        self._dispatch(None)

    def do_POST(self) -> None:
        if self.headers.get_all("Transfer-Encoding", []):
            self._problem(400, "invalid_request", "Invalid routing request.")
            return
        lengths = self.headers.get_all("Content-Length", [])
        content_types = self.headers.get_all("Content-Type", [])
        if len(lengths) != 1 or len(content_types) != 1:
            self._problem(400, "invalid_request", "Invalid routing request.")
            return
        if content_types[0].lower() not in {"application/json", "application/json; charset=utf-8"}:
            self._problem(400, "invalid_request", "Invalid routing request.")
            return
        try:
            length = int(lengths[0])
        except (TypeError, ValueError):
            self._problem(400, "invalid_request", "Invalid routing request.")
            return
        if str(length) != lengths[0] or length < 1:
            self._problem(400, "invalid_request", "Invalid routing request.")
            return
        if length > MAX_REQUEST_BYTES:
            self._problem(413, "request_too_large", "Request is too large.")
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._problem(400, "invalid_request", "Invalid routing request.")
            return
        self._dispatch(body)

    def _dispatch(self, body: bytes | None) -> None:
        status, payload = self.server.router.dispatch(self.command, self.path, body)
        self._send(status, payload)

    def _problem(self, status: int, code: str, message: str) -> None:
        self._send(status, _compact(_problem_wire(RoutingAPIProblem(status, code, message))))

    def _send(self, status: int, payload: bytes) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        del message, explain
        status = 413 if code in {414, 431} else 400
        self.request_version = "HTTP/1.1"
        self._problem(
            status,
            "request_too_large" if status == 413 else "invalid_request",
            "Request is too large." if status == 413 else "Invalid HTTP request.",
        )

    def log_message(self, _format: str, *args: object) -> None:
        return


class RoutingUnixHTTPServer(
    _BoundedThreads, socketserver.ThreadingMixIn, socketserver.UnixStreamServer
):
    allow_reuse_address = False
    request_queue_size = DEFAULT_MAX_THREADS

    def __init__(
        self,
        path: Path,
        router: RoutingAPIRouter,
        *,
        max_threads: int,
        client_timeout: float,
        request_deadline: float,
    ) -> None:
        self.path = path
        self.router = router
        self._created_identity: tuple[int, int] | None = None
        _prepare_socket_path(path)
        try:
            super().__init__(str(path), RoutingHandler)
            current = path.lstat()
            self._created_identity = (current.st_dev, current.st_ino)
            os.chmod(path, 0o600, follow_symlinks=False)
            self._configure_threads(max_threads, client_timeout, request_deadline)
        except Exception:
            if self._created_identity is not None:
                _unlink_socket_if(path, self._created_identity)
            raise

    def verify_request(self, request: socket.socket, client_address: object) -> bool:
        return _peer_is_current_user(request)

    def process_request(self, request, client_address) -> None:
        if not self._thread_slots.acquire(blocking=False):
            payload = _compact(
                _problem_wire(
                    RoutingAPIProblem(503, "router_busy", "Routing is busy.")
                )
            )
            framed = (
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                b"Cache-Control: no-store\r\nConnection: close\r\nContent-Length: "
                + str(len(payload)).encode("ascii")
                + b"\r\n\r\n"
                + payload
            )
            try:
                request.sendall(framed)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            socketserver.ThreadingMixIn.process_request(self, request, client_address)
        except Exception:
            self._thread_slots.release()
            raise

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            _unlink_socket_if(self.path, self._created_identity)
            self._created_identity = None


def create_routing_unix_server(
    socket_path: str | Path,
    controller: RoutingController,
    *,
    max_threads: int = DEFAULT_MAX_THREADS,
    client_timeout: float = DEFAULT_CLIENT_TIMEOUT,
    request_deadline: float = DEFAULT_REQUEST_DEADLINE,
) -> RoutingUnixHTTPServer:
    if isinstance(max_threads, bool) or not isinstance(max_threads, int) or not 1 <= max_threads <= 256:
        raise ValueError("max_threads must be between 1 and 256")
    if isinstance(client_timeout, bool) or not isinstance(client_timeout, (int, float)) or not 0.1 <= client_timeout <= 60:
        raise ValueError("client_timeout must be between 0.1 and 60 seconds")
    if isinstance(request_deadline, bool) or not isinstance(request_deadline, (int, float)) or not 0.05 <= request_deadline <= 300:
        raise ValueError("request_deadline must be between 0.05 and 300 seconds")
    return RoutingUnixHTTPServer(
        Path(socket_path),
        RoutingAPIRouter(controller),
        max_threads=max_threads,
        client_timeout=float(client_timeout),
        request_deadline=float(request_deadline),
    )
