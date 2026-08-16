"""Closed public contracts for the private Plugin API v1."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..provider_ids import OPENUSAGE_PROVIDER_IDS as EXPECTED_PROVIDER_IDS


API_VERSION = "plugin.openusage/v1"
PRINCIPALS = ("loom", "codex", "claude_code", "desktop")
EXTERNAL_PRINCIPALS = PRINCIPALS[:3]
CAPABILITY_IDS = (
    "decision.lookup", "health.query", "outcome.record", "quotas.query",
    "route.advice", "usage.query",
)
MAX_BODY_BYTES = 64 * 1024
MAX_IDEMPOTENCY_RECORDS_PER_PRINCIPAL = 10_000
IDEMPOTENCY_RETENTION_SECONDS = 7 * 24 * 60 * 60
IDEMPOTENCY_KEY_PATTERN = re.compile(r"idem_[0-9a-f]{32}")
DECISION_ID_PATTERN = re.compile(r"decision_[0-9a-f]{32}")
_FIXED_UTC_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z"
)
_SAFE_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")
_WINDOWS = frozenset({"1m", "5m", "15m", "30m", "1h", "6h", "24h", "7d"})
ADVICE_REASONS = frozenset(
    {"approaching_limit", "burn_rate_too_high", "quota_healthy", "quota_low", "quota_unknown"}
)
OUTCOME_REASONS: dict[str, frozenset[str | None]] = {
    "succeeded": frozenset({None}),
    "failed": frozenset({"tool_error", "timeout", "dependency_unavailable", "unknown"}),
    "cancelled": frozenset({"cancelled_by_user"}),
    "ignored": frozenset({"advice_ignored"}),
}
ERRORS: dict[str, tuple[str, bool]] = {
    "invalid_request": ("Invalid request.", False),
    "invalid_target": ("Invalid request target.", False),
    "invalid_header": ("Invalid request header.", False),
    "invalid_idempotency_key": ("A valid Idempotency-Key is required.", False),
    "authentication_required": ("Authentication is required.", False),
    "forbidden_host": ("Host is not allowed.", False),
    "insufficient_scope": ("Principal is not permitted.", False),
    "not_found": ("Route was not found.", False),
    "decision_not_found": ("Decision was not found.", False),
    "method_not_allowed": ("Method is not allowed.", False),
    "idempotency_conflict": ("Idempotency key was used with a different request.", False),
    "idempotency_in_progress": ("The original request is still in progress.", True),
    "decision_outcome_conflict": ("Decision already has a different terminal outcome.", False),
    "request_body_not_allowed": ("Request bodies are not allowed.", False),
    "request_too_large": ("Request is too large.", False),
    "rate_limited": ("Request rate limit exceeded.", True),
    "internal_error": ("Request could not be completed.", True),
    "dependency_unavailable": ("Required local service is unavailable.", True),
    "idempotency_capacity_exceeded": ("Idempotency capacity is unavailable.", True),
    "result_too_large": ("Query result exceeds the public response limit.", False),
}
ERROR_STATUSES: dict[str, frozenset[int]] = {
    "invalid_request": frozenset({400}), "invalid_target": frozenset({400}),
    "invalid_header": frozenset({400}), "invalid_idempotency_key": frozenset({400}),
    "authentication_required": frozenset({401}), "forbidden_host": frozenset({403}),
    "insufficient_scope": frozenset({403}), "not_found": frozenset({404}),
    "decision_not_found": frozenset({404}), "method_not_allowed": frozenset({405}),
    "idempotency_conflict": frozenset({409}), "idempotency_in_progress": frozenset({409}),
    "decision_outcome_conflict": frozenset({409}), "request_body_not_allowed": frozenset({413}),
    "request_too_large": frozenset({413}), "rate_limited": frozenset({429}),
    "internal_error": frozenset({500}), "dependency_unavailable": frozenset({503}),
    "idempotency_capacity_exceeded": frozenset({503}),
    "result_too_large": frozenset({422}),
}


class ContractError(ValueError):
    pass


def utc_fixed6(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ContractError("invalid timestamp")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_fixed6_utc(value: object) -> str:
    if type(value) is not str or _FIXED_UTC_PATTERN.fullmatch(value) is None:
        raise ContractError("invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ContractError("invalid timestamp") from None
    if utc_fixed6(parsed) != value:
        raise ContractError("invalid timestamp")
    return value


def strict_json_object(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_BODY_BYTES:
        raise ContractError("invalid request")

    def reject_constant(_value: str) -> None:
        raise ContractError("invalid request")

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ContractError("invalid request")
            result[key] = value
        return result

    try:
        decoded = raw.decode("utf-8", "strict")
        value = json.loads(decoded, parse_constant=reject_constant, object_pairs_hook=object_pairs)
    except (UnicodeError, json.JSONDecodeError, ContractError, RecursionError):
        raise ContractError("invalid request") from None
    if type(value) is not dict:
        raise ContractError("invalid request")
    return value


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def validate_advice_request(payload: object) -> dict[str, object]:
    fields = {"apiVersion", "provider", "model", "estimatedTokens", "window"}
    if type(payload) is not dict or set(payload) != fields or payload.get("apiVersion") != API_VERSION:
        raise ContractError("invalid request")
    provider = payload["provider"]
    model = payload["model"]
    estimated = payload["estimatedTokens"]
    window = payload["window"]
    if type(provider) is not str or provider not in EXPECTED_PROVIDER_IDS:
        raise ContractError("invalid request")
    if (
        type(model) is not str or not 1 <= len(model) <= 256
        or model != model.strip() or _SAFE_TEXT.fullmatch(model) is None
        or any(unicodedata.category(character).startswith("C") for character in model)
    ):
        raise ContractError("invalid request")
    if type(estimated) is not int or not 1 <= estimated <= 2**31 - 1 or window not in _WINDOWS:
        raise ContractError("invalid request")
    return dict(payload)


def validate_outcome_request(payload: object) -> dict[str, object]:
    fields = {"apiVersion", "decisionId", "outcome", "reason", "occurredAt"}
    if type(payload) is not dict or set(payload) != fields or payload.get("apiVersion") != API_VERSION:
        raise ContractError("invalid request")
    decision_id = payload["decisionId"]
    outcome = payload["outcome"]
    reason = payload["reason"]
    if type(decision_id) is not str or DECISION_ID_PATTERN.fullmatch(decision_id) is None:
        raise ContractError("invalid request")
    if type(outcome) is not str or outcome not in OUTCOME_REASONS or reason not in OUTCOME_REASONS[outcome]:
        raise ContractError("invalid request")
    parse_fixed6_utc(payload["occurredAt"])
    return dict(payload)


def validate_health_request(payload: object) -> dict[str, object]:
    if type(payload) is not dict or payload != {"apiVersion": API_VERSION}:
        raise ContractError("invalid request")
    return dict(payload)


def validate_usage_request(payload: object) -> dict[str, object]:
    if type(payload) is not dict or set(payload) != {"apiVersion", "from", "to"} or payload.get("apiVersion") != API_VERSION:
        raise ContractError("invalid request")
    parsed_days = []
    for name in ("from", "to"):
        value = payload[name]
        if type(value) is not str:
            raise ContractError("invalid request")
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            raise ContractError("invalid request") from None
        if parsed.isoformat() != value:
            raise ContractError("invalid request")
        parsed_days.append(parsed)
    if payload["from"] > payload["to"]:
        raise ContractError("invalid request")
    if (parsed_days[1] - parsed_days[0]).days + 1 > 31:
        raise ContractError("invalid request")
    return dict(payload)


def validate_quotas_request(payload: object) -> dict[str, object]:
    if type(payload) is not dict or set(payload) != {"apiVersion", "limit"} or payload.get("apiVersion") != API_VERSION:
        raise ContractError("invalid request")
    limit = payload["limit"]
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ContractError("invalid request")
    return dict(payload)


def finite_number(value: object, *, minimum: float = 0.0, maximum: float | None = None) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError("invalid dependency response")
    if value < minimum or (maximum is not None and value > maximum):
        raise ContractError("invalid dependency response")
    return value


def problem(code: str, message: str, retryable: bool) -> dict[str, object]:
    return {
        "apiVersion": API_VERSION,
        "object": "plugin.error",
        "error": {"code": code, "message": message, "retryable": retryable},
    }


def _exact_dict(value: object, fields: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ContractError("invalid response")
    return value


def _safe_identifier(value: object, maximum: int = 256) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum or _SAFE_TEXT.fullmatch(value) is None:
        raise ContractError("invalid response")
    return value


def _nullable_text(value: object, maximum: int = 256) -> str | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > maximum or value != value.strip() or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise ContractError("invalid response")
    return value


def _safe_integer(value: object, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 9_007_199_254_740_991:
        raise ContractError("invalid response")
    return value


def _decision(value: object) -> dict[str, object]:
    item = _exact_dict(value, {
        "decisionId", "createdAt", "execution", "outcome", "confidence",
        "reason", "deferUntil", "factsWindow", "details",
    })
    decision_id = item["decisionId"]
    if type(decision_id) is not str or DECISION_ID_PATTERN.fullmatch(decision_id) is None:
        raise ContractError("invalid response")
    created_at = parse_fixed6_utc(item["createdAt"])
    if item["execution"] != "advice_only" or item["outcome"] not in {"yes", "no", "defer"}:
        raise ContractError("invalid response")
    confidence = finite_number(item["confidence"], maximum=1.0)
    reason = item["reason"]
    if reason not in ADVICE_REASONS:
        raise ContractError("invalid response")
    defer_until = item["deferUntil"]
    if defer_until is not None:
        defer_until = parse_fixed6_utc(defer_until)
    if item["outcome"] != "defer" and defer_until is not None:
        raise ContractError("invalid response")
    facts = _exact_dict(item["factsWindow"], {"durationSeconds"})
    duration = _safe_integer(facts["durationSeconds"], 1)
    details = _exact_dict(item["details"], {
        "quotaRemaining", "burnRatePerMin", "predictedExhaustionMinutes",
    })
    projected_details: dict[str, int | float | None] = {}
    for name in ("quotaRemaining", "burnRatePerMin", "predictedExhaustionMinutes"):
        nested = details[name]
        projected_details[name] = None if nested is None else finite_number(nested)
    return {
        "decisionId": decision_id, "createdAt": created_at,
        "execution": "advice_only", "outcome": item["outcome"],
        "confidence": confidence, "reason": reason, "deferUntil": defer_until,
        "factsWindow": {"durationSeconds": duration}, "details": projected_details,
    }


def _receipt(value: object) -> dict[str, object]:
    item = _exact_dict(value, {"decisionId", "outcome", "reason", "occurredAt", "recordedAt"})
    decision_id = item["decisionId"]
    outcome = item["outcome"]
    reason = item["reason"]
    if type(decision_id) is not str or DECISION_ID_PATTERN.fullmatch(decision_id) is None:
        raise ContractError("invalid response")
    if type(outcome) is not str or outcome not in OUTCOME_REASONS or reason not in OUTCOME_REASONS[outcome]:
        raise ContractError("invalid response")
    return {
        "decisionId": decision_id, "outcome": outcome, "reason": reason,
        "occurredAt": parse_fixed6_utc(item["occurredAt"]),
        "recordedAt": parse_fixed6_utc(item["recordedAt"]),
    }


def _error_response(payload: object) -> dict[str, object]:
    root = _exact_dict(payload, {"apiVersion", "object", "error"})
    error = _exact_dict(root["error"], {"code", "message", "retryable"})
    code = error["code"]
    if root["apiVersion"] != API_VERSION or root["object"] != "plugin.error" or type(code) is not str or code not in ERRORS:
        raise ContractError("invalid response")
    message, retryable = ERRORS[code]
    if error["message"] != message or error["retryable"] is not retryable:
        raise ContractError("invalid response")
    return {"apiVersion": API_VERSION, "object": "plugin.error", "error": {"code": code, "message": message, "retryable": retryable}}


def sanitize_response(route: str, status: int, payload: object) -> dict[str, object]:
    """Return an exact deep public projection or fail closed.

    This function is intentionally pure and is shared by the HTTP listener and
    fixed stdio bridges. It never imports the API router, databases, or Gateway.
    """
    if type(route) is not str or type(status) is not int or type(payload) is not dict:
        raise ContractError("invalid response")
    if status >= 400:
        sanitized = _error_response(payload)
        if status not in ERROR_STATUSES[str(sanitized["error"]["code"])]:
            raise ContractError("invalid response")
        return sanitized
    if status != 200:
        raise ContractError("invalid response")
    if route == "/plugin/v1/schema":
        try:
            expected = json.loads(
                (Path(__file__).resolve().parents[1] / "resources/plugin-api-v1.schema.json").read_text(encoding="utf-8"),
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except Exception:
            raise ContractError("invalid response") from None
        if payload != expected:
            raise ContractError("invalid response")
        return json.loads(canonical_json(expected))
    if route == "/plugin/v1/capabilities":
        root = _exact_dict(payload, {"apiVersion", "object", "principal", "capabilities"})
        if root["apiVersion"] != API_VERSION or root["object"] != "plugin.capabilities" or root["principal"] not in EXTERNAL_PRINCIPALS or root["capabilities"] != list(CAPABILITY_IDS):
            raise ContractError("invalid response")
        return {"apiVersion": API_VERSION, "object": "plugin.capabilities", "principal": root["principal"], "capabilities": list(CAPABILITY_IDS)}
    if route == "/plugin/v1/route-advice":
        root = _exact_dict(payload, {"apiVersion", "object", "decision"})
        if root["apiVersion"] != API_VERSION or root["object"] != "plugin.route_advice":
            raise ContractError("invalid response")
        return {"apiVersion": API_VERSION, "object": "plugin.route_advice", "decision": _decision(root["decision"])}
    if route == "/plugin/v1/outcomes":
        root = _exact_dict(payload, {"apiVersion", "object", "receipt"})
        if root["apiVersion"] != API_VERSION or root["object"] != "plugin.outcome_receipt":
            raise ContractError("invalid response")
        return {"apiVersion": API_VERSION, "object": "plugin.outcome_receipt", "receipt": _receipt(root["receipt"])}
    if route.startswith("/plugin/v1/decisions/"):
        root = _exact_dict(payload, {"apiVersion", "object", "decision", "outcome"})
        if root["apiVersion"] != API_VERSION or root["object"] != "plugin.decision":
            raise ContractError("invalid response")
        outcome = None if root["outcome"] is None else _receipt(root["outcome"])
        return {"apiVersion": API_VERSION, "object": "plugin.decision", "decision": _decision(root["decision"]), "outcome": outcome}
    if route == "/plugin/v1/health/query":
        root = _exact_dict(payload, {"apiVersion", "object", "observedAt", "listener", "observerApi", "gatewayApi"})
        if root["apiVersion"] != API_VERSION or root["object"] != "plugin.health" or root["listener"] != "ready" or root["observerApi"] not in {"ready", "unavailable", "unknown"} or root["gatewayApi"] not in {"ready", "disabled", "unavailable", "unknown"}:
            raise ContractError("invalid response")
        return {"apiVersion": API_VERSION, "object": "plugin.health", "observedAt": parse_fixed6_utc(root["observedAt"]), "listener": "ready", "observerApi": root["observerApi"], "gatewayApi": root["gatewayApi"]}
    if route == "/plugin/v1/connections":
        return _connections_response(payload)
    if route == "/plugin/v1/usage/query":
        return _usage_response(payload)
    if route == "/plugin/v1/quotas/query":
        return _quotas_response(payload)
    raise ContractError("invalid response")


def _connections_response(payload: object) -> dict[str, object]:
    root = _exact_dict(payload, {"apiVersion", "object", "observedAt", "connections"})
    if root["apiVersion"] != API_VERSION or root["object"] != "plugin.connections" or type(root["connections"]) is not list or len(root["connections"]) != 3:
        raise ContractError("invalid response")
    rows: list[dict[str, object]] = []
    for expected_id, value in zip(EXTERNAL_PRINCIPALS, root["connections"], strict=True):
        item = _exact_dict(value, {"pluginId", "configuration", "connection", "capabilityState", "capabilities", "lastSeenAt", "lastSyncOutcome", "lastSyncAt"})
        if (
            item["pluginId"] != expected_id
            or item["configuration"] not in {"configured", "not_configured", "unknown"}
            or item["connection"] not in {"never_seen", "connected", "stale", "unknown"}
            or item["capabilityState"] not in {"not_negotiated", "negotiated", "incompatible", "unknown"}
            or item["lastSyncOutcome"] not in {"never", "succeeded", "failed", "unknown"}
            or type(item["capabilities"]) is not list
        ):
            raise ContractError("invalid response")
        capabilities = list(item["capabilities"])
        if capabilities != sorted(set(capabilities)) or any(value not in CAPABILITY_IDS for value in capabilities):
            raise ContractError("invalid response")
        last_seen = None if item["lastSeenAt"] is None else parse_fixed6_utc(item["lastSeenAt"])
        last_sync = None if item["lastSyncAt"] is None else parse_fixed6_utc(item["lastSyncAt"])
        if (item["lastSyncOutcome"] in {"succeeded", "failed"}) != (last_sync is not None):
            raise ContractError("invalid response")
        if item["capabilityState"] != "negotiated" and capabilities:
            raise ContractError("invalid response")
        if item["capabilityState"] == "negotiated" and capabilities != sorted(CAPABILITY_IDS):
            raise ContractError("invalid response")
        if item["connection"] == "never_seen" and last_seen is not None:
            raise ContractError("invalid response")
        if item["connection"] in {"connected", "stale"} and (item["configuration"] != "configured" or last_seen is None):
            raise ContractError("invalid response")
        if item["configuration"] == "not_configured" and (
            item["connection"] != "never_seen" or item["capabilityState"] != "not_negotiated"
            or capabilities or last_seen is not None or item["lastSyncOutcome"] != "never" or last_sync is not None
        ):
            raise ContractError("invalid response")
        rows.append({
            "pluginId": expected_id, "configuration": item["configuration"],
            "connection": item["connection"], "capabilityState": item["capabilityState"],
            "capabilities": capabilities, "lastSeenAt": last_seen,
            "lastSyncOutcome": item["lastSyncOutcome"], "lastSyncAt": last_sync,
        })
    return {"apiVersion": API_VERSION, "object": "plugin.connections", "observedAt": parse_fixed6_utc(root["observedAt"]), "connections": rows}


def _usage_response(payload: object) -> dict[str, object]:
    root = _exact_dict(payload, {"apiVersion", "object", "dataRevision", "generatedAt", "from", "to", "rows"})
    if root["apiVersion"] != API_VERSION or root["object"] != "plugin.usage" or type(root["rows"]) is not list or len(root["rows"]) > 1000:
        raise ContractError("invalid response")
    validate_usage_request({"apiVersion": API_VERSION, "from": root["from"], "to": root["to"]})
    rows: list[dict[str, object]] = []
    fields = {"day", "providerId", "modelId", "inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens", "reasoningTokens", "totalTokens", "costAmount", "costCurrency", "costBasis", "quality", "modelFamily"}
    for value in root["rows"]:
        item = _exact_dict(value, fields)
        validate_usage_request({"apiVersion": API_VERSION, "from": item["day"], "to": item["day"]})
        provider = item["providerId"]
        if provider not in EXPECTED_PROVIDER_IDS:
            raise ContractError("invalid response")
        row = {"day": item["day"], "providerId": provider, "modelId": _safe_identifier(item["modelId"])}
        for name in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens", "totalTokens"):
            row[name] = _safe_integer(item[name])
        row["reasoningTokens"] = None if item["reasoningTokens"] is None else _safe_integer(item["reasoningTokens"])
        for name in ("costAmount", "costCurrency", "costBasis"):
            row[name] = _nullable_text(item[name], 64)
        row["quality"] = _safe_identifier(item["quality"], 64)
        row["modelFamily"] = _safe_identifier(item["modelFamily"], 128)
        rows.append(row)
    return {"apiVersion": API_VERSION, "object": "plugin.usage", "dataRevision": _safe_integer(root["dataRevision"]), "generatedAt": parse_fixed6_utc(root["generatedAt"]), "from": root["from"], "to": root["to"], "rows": rows}


def _quotas_response(payload: object) -> dict[str, object]:
    root = _exact_dict(payload, {"apiVersion", "object", "selection", "dataRevision", "generatedAt", "quotas"})
    if root["apiVersion"] != API_VERSION or root["object"] != "plugin.quotas" or root["selection"] != "most_constrained_observed_scope" or type(root["quotas"]) is not list or len(root["quotas"]) > 128:
        raise ContractError("invalid response")
    fields = {"providerId", "quotaName", "unit", "used", "limit", "remaining", "remainingRatio", "resetsAt", "periodStart", "periodEnd", "observedAt", "freshnessSeconds", "state", "quality", "stale", "quotaWindow"}
    rows: list[dict[str, object]] = []
    for value in root["quotas"]:
        item = _exact_dict(value, fields)
        if item["providerId"] not in EXPECTED_PROVIDER_IDS or type(item["stale"]) is not bool:
            raise ContractError("invalid response")
        row: dict[str, object] = {"providerId": item["providerId"]}
        for name in ("quotaName", "unit", "state", "quality", "quotaWindow"):
            row[name] = _safe_identifier(item[name], 128)
        for name in ("used", "limit", "remaining"):
            row[name] = _nullable_text(item[name], 128)
        ratio = item["remainingRatio"]
        row["remainingRatio"] = None if ratio is None else finite_number(ratio, maximum=1.0)
        for name in ("resetsAt", "periodStart", "periodEnd"):
            row[name] = None if item[name] is None else parse_fixed6_utc(item[name])
        row["observedAt"] = parse_fixed6_utc(item["observedAt"])
        row["freshnessSeconds"] = _safe_integer(item["freshnessSeconds"])
        row["stale"] = item["stale"]
        rows.append(row)
    return {"apiVersion": API_VERSION, "object": "plugin.quotas", "selection": "most_constrained_observed_scope", "dataRevision": _safe_integer(root["dataRevision"]), "generatedAt": parse_fixed6_utc(root["generatedAt"]), "quotas": rows}
