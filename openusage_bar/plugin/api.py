"""Strict, principal-scoped router for Plugin API v1."""

from __future__ import annotations

import copy
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..gateway.decision_trace import facts_window_duration_seconds
from .contracts import (
    ADVICE_REASONS,
    API_VERSION,
    CAPABILITY_IDS,
    DECISION_ID_PATTERN,
    EXTERNAL_PRINCIPALS,
    IDEMPOTENCY_KEY_PATTERN,
    PRINCIPALS,
    ContractError,
    finite_number,
    parse_fixed6_utc,
    problem,
    strict_json_object,
    sanitize_response,
    utc_fixed6,
    validate_advice_request,
    validate_health_request,
    validate_outcome_request,
    validate_quotas_request,
    validate_usage_request,
)
from .store import (
    DecisionNotFound,
    DecisionOutcomeConflict,
    IdempotencyCapacityExceeded,
    IdempotencyConflict,
    IdempotencyInProgress,
    PluginStore,
)
from .clients import ResultTooLarge


_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "resources/plugin-api-v1.schema.json"
_GET_ROUTES = frozenset({
    "/plugin/v1/schema", "/plugin/v1/capabilities", "/plugin/v1/connections",
})
_POST_ROUTES = frozenset({
    "/plugin/v1/usage/query", "/plugin/v1/quotas/query", "/plugin/v1/health/query",
    "/plugin/v1/route-advice", "/plugin/v1/outcomes",
})
_IDEMPOTENT_ROUTES = frozenset({"/plugin/v1/route-advice", "/plugin/v1/outcomes"})
_EXTERNAL_SCOPES = frozenset(_GET_ROUTES - {"/plugin/v1/connections"}) | _POST_ROUTES
_STALE_AFTER = timedelta(minutes=5)


def _load_schema() -> dict[str, object]:
    try:
        value = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"apiVersion": API_VERSION, "object": "plugin.schema", "available": False}
    return value if type(value) is dict else {"apiVersion": API_VERSION, "object": "plugin.schema", "available": False}


class PluginRouter:
    def __init__(
        self, *, store: PluginStore, facts_client: object, advice_client: object,
        configured_principals: tuple[str, ...] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(store) is not PluginStore:
            raise ValueError("invalid Plugin store")
        if type(configured_principals) is not tuple or any(
            item not in PRINCIPALS for item in configured_principals
        ):
            raise ValueError("invalid Plugin principals")
        self._store = store
        self._facts = facts_client
        self._advice = advice_client
        self._configured = frozenset(configured_principals)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def close(self) -> None:
        self._store.close()

    def dispatch(
        self, principal: str, method: str, path: str, body: bytes,
        *, idempotency_key: str | None = None,
    ) -> tuple[int, dict[str, object]]:
        if principal not in PRINCIPALS:
            return 401, problem("authentication_required", "Authentication is required.", False)
        if type(method) is not str or type(path) is not str or type(body) is not bytes:
            return 400, problem("invalid_request", "Invalid request.", False)
        if "?" in path or "#" in path or "%" in path:
            return 400, problem("invalid_target", "Invalid request target.", False)
        is_decision = path.startswith("/plugin/v1/decisions/") and len(path.split("/")) == 5
        known_path = path in _GET_ROUTES or path in _POST_ROUTES or is_decision
        if not known_path:
            return 404, problem("not_found", "Route was not found.", False)
        expected_method = "GET" if path in _GET_ROUTES or is_decision else "POST"
        if method != expected_method:
            return 405, problem("method_not_allowed", "Method is not allowed.", False)
        if principal == "desktop":
            if path != "/plugin/v1/connections":
                return 403, problem("insufficient_scope", "Principal is not permitted.", False)
        elif path not in _EXTERNAL_SCOPES and not is_decision:
            return 403, problem("insufficient_scope", "Principal is not permitted.", False)
        if method == "GET":
            if body:
                return 413, problem("request_body_not_allowed", "Request bodies are not allowed.", False)
            if idempotency_key is not None:
                return 400, problem("invalid_header", "Invalid request header.", False)
            return self._get(principal, path)
        if path in _IDEMPOTENT_ROUTES:
            if type(idempotency_key) is not str or IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key) is None:
                return 400, problem("invalid_idempotency_key", "A valid Idempotency-Key is required.", False)
        elif idempotency_key is not None:
            return 400, problem("invalid_header", "Invalid request header.", False)
        try:
            payload = strict_json_object(body)
        except ContractError:
            return 400, problem("invalid_request", "Invalid request.", False)
        return self._post(principal, path, payload, idempotency_key)

    def _get(self, principal: str, path: str) -> tuple[int, dict[str, object]]:
        if path == "/plugin/v1/schema":
            self._store.observe_principal(principal)
            return 200, sanitize_response(path, 200, copy.deepcopy(_load_schema()))
        if path == "/plugin/v1/capabilities":
            self._store.observe_principal(principal, negotiated=True)
            payload = {
                "apiVersion": API_VERSION,
                "object": "plugin.capabilities",
                "principal": principal,
                "capabilities": list(CAPABILITY_IDS),
            }
            return 200, sanitize_response(path, 200, payload)
        if path == "/plugin/v1/connections":
            return 200, sanitize_response(path, 200, self._connections())
        decision_id = path.rsplit("/", 1)[-1]
        if DECISION_ID_PATTERN.fullmatch(decision_id) is None:
            return 404, problem("decision_not_found", "Decision was not found.", False)
        self._store.observe_principal(principal)
        try:
            decision, outcome = self._store.get_decision(principal, decision_id)
        except DecisionNotFound:
            return 404, problem("decision_not_found", "Decision was not found.", False)
        self._store.observe_principal(principal)
        payload = {
            "apiVersion": API_VERSION, "object": "plugin.decision",
            "decision": decision, "outcome": outcome,
        }
        return 200, sanitize_response(path, 200, payload)

    def _post(
        self, principal: str, path: str, payload: dict[str, Any],
        key: str | None,
    ) -> tuple[int, dict[str, object]]:
        try:
            if path == "/plugin/v1/route-advice":
                validated = validate_advice_request(payload)
                self._store.observe_principal(principal)
                assert key is not None
                return self._idempotent(
                    principal, path, key, validated,
                    lambda: self._create_advice(principal, validated),
                )
            if path == "/plugin/v1/outcomes":
                validated = validate_outcome_request(payload)
                self._store.observe_principal(principal)
                assert key is not None
                return self._idempotent(
                    principal, path, key, validated,
                    lambda: self._record_outcome(principal, validated),
                )
            if path == "/plugin/v1/health/query":
                validated = validate_health_request(payload)
                self._store.observe_principal(principal)
                response = self._health(validated)
            elif path == "/plugin/v1/usage/query":
                validated = validate_usage_request(payload)
                self._store.observe_principal(principal)
                response = self._query("usage", validated)
            else:
                validated = validate_quotas_request(payload)
                self._store.observe_principal(principal)
                response = self._query("quotas", validated)
        except ContractError:
            return 400, problem("invalid_request", "Invalid request.", False)
        except ResultTooLarge:
            self._store.observe_principal(principal, outcome="failed")
            return 422, problem("result_too_large", "Query result exceeds the public response limit.", False)
        except Exception:
            self._store.observe_principal(principal, outcome="failed")
            return 503, problem("dependency_unavailable", "Required local service is unavailable.", True)
        self._store.observe_principal(principal, outcome="succeeded")
        return 200, response

    def _idempotent(
        self, principal: str, route: str, key: str, projection: dict[str, object],
        operation: Callable[[], tuple[int, dict[str, object]] | tuple[int, dict[str, object], Callable[[object], None]]],
    ) -> tuple[int, dict[str, object]]:
        def safe_operation() -> tuple[int, dict[str, object]] | tuple[
            int, dict[str, object], Callable[[object], None]
        ]:
            try:
                return operation()
            except DecisionNotFound:
                return 404, problem("decision_not_found", "Decision was not found.", False)
            except DecisionOutcomeConflict:
                return 409, problem("decision_outcome_conflict", "Decision already has a different terminal outcome.", False)
            except Exception:
                return 503, problem("dependency_unavailable", "Required local service is unavailable.", True)

        try:
            result = self._store.execute_idempotent(
                principal=principal, route=route, key=key,
                projection=projection, operation=safe_operation,
            )
        except IdempotencyConflict:
            return 409, problem("idempotency_conflict", "Idempotency key was used with a different request.", False)
        except IdempotencyInProgress:
            return 409, problem("idempotency_in_progress", "The original request is still in progress.", True)
        except IdempotencyCapacityExceeded:
            return 503, problem("idempotency_capacity_exceeded", "Idempotency capacity is unavailable.", True)
        except Exception:
            return 500, problem("internal_error", "Request could not be completed.", True)
        return result

    def _create_advice(
        self, principal: str, request: dict[str, object]
    ) -> tuple[int, dict[str, object], Callable[[object], None]]:
        gateway_request = {
            "provider": request["provider"], "model": request["model"],
            "estimated_tokens": request["estimatedTokens"], "window": request["window"],
        }
        candidate = self._advice.should_send(gateway_request)
        decision = self._validated_gateway_decision(candidate, str(request["window"]))
        decision["decisionId"] = "decision_" + secrets.token_hex(16)
        decision["createdAt"] = utc_fixed6(self._now())
        ordered = {
            "decisionId": decision["decisionId"], "createdAt": decision["createdAt"],
            "execution": "advice_only", "outcome": decision["outcome"],
            "confidence": decision["confidence"], "reason": decision["reason"],
            "deferUntil": decision["deferUntil"], "factsWindow": decision["factsWindow"],
            "details": decision["details"],
        }
        response = {"apiVersion": API_VERSION, "object": "plugin.route_advice", "decision": ordered}
        response = sanitize_response("/plugin/v1/route-advice", 200, response)

        def mutation(connection: object) -> None:
            self._store.insert_decision_in_transaction(connection, principal, ordered)

        return 200, response, mutation

    def _validated_gateway_decision(self, value: object, window: str) -> dict[str, object]:
        if type(value) is not dict or set(value) != {"decision", "confidence", "reason", "defer_until", "details"}:
            raise RuntimeError("invalid Gateway response")
        outcome = value["decision"]
        reason = value["reason"]
        if outcome not in {"yes", "no", "defer"} or reason not in ADVICE_REASONS:
            raise RuntimeError("invalid Gateway response")
        confidence = finite_number(value["confidence"], maximum=1.0)
        defer = value["defer_until"]
        if defer is not None:
            if type(defer) is not str or len(defer) > 64:
                raise RuntimeError("invalid Gateway response")
            try:
                parsed = datetime.fromisoformat(defer.replace("Z", "+00:00"))
            except ValueError:
                raise RuntimeError("invalid Gateway response") from None
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise RuntimeError("invalid Gateway response")
            defer = utc_fixed6(parsed)
        if outcome != "defer" and defer is not None:
            raise RuntimeError("invalid Gateway response")
        details = value["details"]
        expected = {"quota_remaining", "burn_rate_per_min", "predicted_exhaustion_minutes"}
        if type(details) is not dict or set(details) != expected:
            raise RuntimeError("invalid Gateway response")
        normalized: dict[str, int | float | None] = {}
        for source, target in (
            ("quota_remaining", "quotaRemaining"),
            ("burn_rate_per_min", "burnRatePerMin"),
            ("predicted_exhaustion_minutes", "predictedExhaustionMinutes"),
        ):
            item = details[source]
            normalized[target] = None if item is None else finite_number(item)
        duration = facts_window_duration_seconds(window)
        if duration is None:
            raise RuntimeError("invalid facts window")
        return {
            "outcome": outcome, "confidence": confidence, "reason": reason,
            "deferUntil": defer, "factsWindow": {"durationSeconds": duration},
            "details": normalized,
        }

    def _record_outcome(
        self, principal: str, request: dict[str, object]
    ) -> tuple[int, dict[str, object], Callable[[object], None]]:
        receipt = {
            "decisionId": request["decisionId"], "outcome": request["outcome"],
            "reason": request["reason"], "occurredAt": request["occurredAt"],
            "recordedAt": utc_fixed6(self._now()),
        }
        response = {"apiVersion": API_VERSION, "object": "plugin.outcome_receipt", "receipt": receipt}
        response = sanitize_response("/plugin/v1/outcomes", 200, response)

        def mutation(
            connection: object,
        ) -> tuple[int, dict[str, object]] | None:
            try:
                existing = self._store.record_outcome_in_transaction(
                    connection, principal, str(request["decisionId"]), receipt
                )
            except DecisionNotFound:
                return 404, problem("decision_not_found", "Decision was not found.", False)
            except DecisionOutcomeConflict:
                return 409, problem("decision_outcome_conflict", "Decision already has a different terminal outcome.", False)
            response["receipt"] = existing
            return None

        return 200, response, mutation

    def _health(self, _request: dict[str, object]) -> dict[str, object]:
        observer_status = "unavailable"
        gateway_status = "unavailable"
        try:
            value = self._facts.request("/v1/health", {})
            if type(value) is dict and type(value.get("health")) is dict:
                health = value["health"]
                if set(health) == {"ok", "status"} and health.get("ok") is True and health.get("status") == "ok":
                    observer_status = "ready"
        except Exception:
            pass
        try:
            health_method = getattr(self._advice, "health")
            candidate = health_method()
            if candidate in {"ready", "disabled"}:
                gateway_status = candidate
        except Exception:
            pass
        return sanitize_response("/plugin/v1/health/query", 200, {
            "apiVersion": API_VERSION, "object": "plugin.health",
            "observedAt": utc_fixed6(self._now()), "listener": "ready",
            "observerApi": observer_status, "gatewayApi": gateway_status,
        })

    def _query(self, kind: str, request: dict[str, object]) -> dict[str, object]:
        method = getattr(self._facts, f"query_{kind}", None)
        if not callable(method):
            raise RuntimeError("Local API client unavailable")
        value = method(request)
        if type(value) is not dict:
            raise RuntimeError("invalid Local API response")
        return sanitize_response(f"/plugin/v1/{kind}/query", 200, value)

    def _connections(self) -> dict[str, object]:
        now = self._now()
        rows: list[dict[str, object]] = []
        for principal in EXTERNAL_PRINCIPALS:
            state = self._store.connection_state(principal)
            last_seen = state["lastSeenAt"]
            configured = principal in self._configured or last_seen is not None
            if not configured:
                configuration = "not_configured"
                connection = "never_seen"
                capability_state = "not_negotiated"
                capabilities: list[str] = []
                last_seen = None
                sync_outcome = "never"
                last_sync = None
            elif last_seen is None:
                configuration = "configured"
                connection = "never_seen"
                capability_state = "not_negotiated"
                capabilities = []
                sync_outcome = state["lastSyncOutcome"]
                last_sync = state["lastSyncAt"]
            else:
                configuration = "configured"
                try:
                    seen = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
                    if seen > now:
                        connection = "unknown"
                    else:
                        connection = "connected" if now - seen <= _STALE_AFTER else "stale"
                except ValueError:
                    connection = "unknown"
                negotiated = state["capabilitiesNegotiated"] is True
                capability_state = "negotiated" if negotiated else "not_negotiated"
                capabilities = sorted(CAPABILITY_IDS) if negotiated else []
                sync_outcome = state["lastSyncOutcome"]
                last_sync = state["lastSyncAt"]
            rows.append({
                "pluginId": principal, "configuration": configuration,
                "connection": connection, "capabilityState": capability_state,
                "capabilities": capabilities, "lastSeenAt": last_seen,
                "lastSyncOutcome": sync_outcome, "lastSyncAt": last_sync,
            })
        return {
            "apiVersion": API_VERSION, "object": "plugin.connections",
            "observedAt": utc_fixed6(now), "connections": rows,
        }

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("Plugin clock is invalid")
        return value.astimezone(timezone.utc)
