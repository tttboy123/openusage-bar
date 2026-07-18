#!/usr/bin/env python3
"""Export an explicit, aggregate-only OpenUsage Bar canary diagnostic."""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import re
import socket
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
API_SCHEMA = "1.0"
DIAGNOSTIC_SCHEMA = "openusage-diagnostics-1"
RECONCILIATION_SCHEMA = "openusage-diagnostics-2"
MAX_ACTIVITY_RANGE_DAYS = 731
IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SOURCE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SAFE_SOURCE_ERROR_CODES = frozenset({
    "AMBIGUOUS_FALLBACK_SCOPE",
    "AUTH_EXPIRED",
    "AUTH_FAILED",
    "AUTH_REJECTED",
    "AUTH_REQUIRED",
    "AUTHENTICATION_REQUIRED",
    "COMMAND_FAILED",
    "EMPTY_RESULT",
    "IMPORT_FAILED",
    "IMPORT_IN_PROGRESS",
    "INTERNAL_ERROR",
    "INVALID_DETECT_OUTPUT",
    "INVALID_ENVELOPE",
    "INVALID_IMPORT_RESULT",
    "INVALID_IMPORT_ROWS",
    "INVALID_IMPORT_SCOPE",
    "INVALID_JSON",
    "INVALID_OBSERVATION_TIME",
    "INVALID_PAYLOAD",
    "INVALID_REQUEST",
    "INVALID_RESPONSE",
    "KEYCHAIN_UNAVAILABLE",
    "NETWORK_ERROR",
    "NOT_AVAILABLE_YET",
    "NOT_CHECKED",
    "NOT_COLLECTED",
    "NOT_CONFIGURED",
    "OPENUSAGE_UNAVAILABLE",
    "PERSISTENCE_FAILED",
    "QUOTA_PERSISTENCE_FAILED",
    "QUOTA_UNAVAILABLE",
    "RATE_LIMITED",
    "READER_FAILED",
    "RUNNER_FAILED",
    "SESSIONS_INVALID",
    "SESSIONS_UNAVAILABLE",
    "START_FAILED",
    "TIMED_OUT",
    "TIMEOUT",
    "UNEXPECTED_FAILURE",
    "UNSUPPORTED_OPENUSAGE_VERSION",
})
VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$")
CAPABILITY_STATES = frozenset({"supported", "unsupported", "unknown"})
CAPABILITY_FIELDS = (
    "tokenHistory", "modelBreakdown", "resetTimestamps", "billing", "credits",
    "balance", "cost", "rateLimits", "serviceStatus",
)
FORBIDDEN_KEYS = frozenset({
    "accountref", "displayname", "credentialsource", "sourceid", "payloadjson",
    "apikey", "secret", "cookie", "prompt", "response", "rawpayload",
})
RECONCILIATION_FORBIDDEN_KEYS = FORBIDDEN_KEYS - {"accountref", "sourceid"}
FORBIDDEN_INPUT_KEYS = frozenset({
    "apikey", "api_key", "secret", "token", "password", "cookie", "prompt",
    "response", "payloadjson", "rawpayload", "changejson",
})
SECRET_TEXT = (
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
)
UTC_OFFSET = re.compile(r"^UTC([+-])(\d{2}):(\d{2})$")


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _items(value: Any, name: str, *, limit: int = 10_000) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"{name} must be a bounded array")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a stable identifier")
    return value


def _optional_identifier(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, name)


def _timestamp(value: Any, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError(f"{name} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be a timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    parsed = parsed.astimezone(timezone.utc)
    timespec = "microseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=timespec).replace("+00:00", "Z")


def _day_text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a local calendar day")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a local calendar day") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be a canonical local calendar day")
    return value


def _error_code(value: Any) -> str | None:
    if value is None:
        return None
    normalized = None
    if isinstance(value, str) and ERROR_CODE.fullmatch(value):
        normalized = value
    elif isinstance(value, str) and SOURCE_ERROR_CODE.fullmatch(value):
        normalized = value.upper()
    if normalized in SAFE_SOURCE_ERROR_CODES:
        return normalized
    return "UNCLASSIFIED"


def _timezone_info(name: str) -> tzinfo:
    if name == "UTC":
        return timezone.utc
    offset = UTC_OFFSET.fullmatch(name)
    if offset is not None:
        hours = int(offset.group(2))
        minutes = int(offset.group(3))
        if hours > 14 or minutes > 59 or (hours == 14 and minutes != 0):
            raise ValueError("invalid local timezone")
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(delta if offset.group(1) == "+" else -delta, name)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        raise ValueError("invalid local timezone") from error


def _local_timezone_name() -> str:
    try:
        target = os.readlink("/etc/localtime")
        marker = "/zoneinfo/"
        if marker in target:
            candidate = target.split(marker, 1)[1]
            _timezone_info(candidate)
            return candidate
    except (OSError, ValueError):
        pass
    offset = datetime.now().astimezone().strftime("%z")
    if offset in {"", "+0000", "-0000"}:
        return "UTC"
    return f"UTC{offset[:3]}:{offset[3:]}"


def _identifier_list(value: Any, name: str) -> list[str]:
    result = [_identifier(item, name) for item in _items(value, name, limit=100)]
    return sorted(set(result))


def _counted(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _token_reconciliation(
    *,
    convention: str,
    total_tokens: int,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    reasoning_tokens: int | None,
) -> dict[str, Any]:
    """Compare a source total only when its declared arithmetic is complete."""
    if convention == "unknown":
        return {
            "status": "not_comparable",
            "reason": "unknown_counting_convention",
        }
    if convention == "provider_reported":
        return {
            "status": "not_comparable",
            "reason": "provider_reported_total",
        }
    if convention == "components_disjoint" and reasoning_tokens is None:
        return {
            "status": "incomplete",
            "reason": "reasoning_tokens_missing",
        }
    if convention == "input_includes_cache":
        expected = input_tokens + output_tokens
    else:
        assert convention == "components_disjoint" and reasoning_tokens is not None
        expected = (
            input_tokens
            + output_tokens
            + cache_read_tokens
            + cache_creation_tokens
            + reasoning_tokens
        )
    delta = total_tokens - expected
    return {
        "status": "matched" if delta == 0 else "mismatch",
        "expectedTotalTokens": expected,
        "deltaTokens": delta,
    }


def _capability_declarations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _items(payload.get("providers"), "capability providers", limit=1_000):
        provider = _mapping(raw, "capability provider")
        family_id = _identifier(provider.get("familyId"), "familyId")
        if family_id in seen:
            raise ValueError("duplicate capability family")
        seen.add(family_id)
        raw_capabilities = _mapping(provider.get("capabilities"), "capabilities")
        quota = _mapping(raw_capabilities.get("quotaWindows"), "quotaWindows")
        quota_state = quota.get("state")
        if quota_state not in CAPABILITY_STATES:
            raise ValueError("invalid quota capability state")
        states: dict[str, str] = {}
        for field in CAPABILITY_FIELDS:
            state = raw_capabilities.get(field)
            if state not in CAPABILITY_STATES:
                raise ValueError("invalid capability state")
            states[field] = state
        sources: list[dict[str, str]] = []
        for raw_source in _items(provider.get("sources"), "capability sources", limit=100):
            source = _mapping(raw_source, "capability source")
            sources.append({
                "kind": _identifier(source.get("kind"), "source kind"),
                "provenance": _identifier(source.get("provenance"), "source provenance"),
                "stability": _identifier(source.get("stability"), "source stability"),
            })
        supports_accounts = provider.get("supportsAccounts")
        if not isinstance(supports_accounts, bool):
            raise ValueError("supportsAccounts must be boolean")
        declarations.append({
            "capabilities": {
                "quotaWindows": {
                    "state": quota_state,
                    "values": _identifier_list(quota.get("values"), "quota windows"),
                },
                **states,
            },
            "familyId": family_id,
            "metricFamilies": _identifier_list(
                provider.get("metricFamilies"), "metric families"
            ),
            "regions": _identifier_list(provider.get("regions"), "regions"),
            "sources": sorted(
                sources, key=lambda item: (item["kind"], item["stability"], item["provenance"])
            ),
            "supportsAccounts": supports_accounts,
        })
    return sorted(declarations, key=lambda item: item["familyId"])


def _validate_export(value: Any, *, home: Path | None = None) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key.casefold() in FORBIDDEN_KEYS:
                raise ValueError("diagnostic contains a forbidden field")
            _validate_export(nested, home=home)
    elif isinstance(value, list):
        for nested in value:
            _validate_export(nested, home=home)
    elif isinstance(value, str):
        if any(pattern.search(value) for pattern in SECRET_TEXT):
            raise ValueError("diagnostic contains secret-like text")
        if value.startswith(("/Users/", "/home/")) or (
            home is not None and str(home) in value
        ):
            raise ValueError("diagnostic contains an absolute home path")


def _validate_reconciliation_export(
    value: Any, *, home: Path | None = None
) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key.casefold() in RECONCILIATION_FORBIDDEN_KEYS:
                raise ValueError("diagnostic contains a forbidden field")
            _validate_reconciliation_export(nested, home=home)
    elif isinstance(value, list):
        for nested in value:
            _validate_reconciliation_export(nested, home=home)
    elif isinstance(value, str):
        if any(pattern.search(value) for pattern in SECRET_TEXT):
            raise ValueError("diagnostic contains secret-like text")
        if value.startswith(("/Users/", "/home/")) or (
            home is not None and str(home) in value
        ):
            raise ValueError("diagnostic contains an absolute home path")


def _reject_sensitive_input(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).casefold() in FORBIDDEN_INPUT_KEYS:
                raise ValueError("Local API response contains a forbidden field")
            _reject_sensitive_input(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_input(nested)


def build_diagnostics(
    snapshot: dict[str, Any],
    capabilities: dict[str, Any],
    *,
    product: dict[str, str],
    runtime: dict[str, str],
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    snapshot = _mapping(snapshot, "snapshot")
    capabilities = _mapping(capabilities, "capabilities")
    _reject_sensitive_input(snapshot)
    _reject_sensitive_input(capabilities)
    if snapshot.get("schemaVersion") != API_SCHEMA or capabilities.get("schemaVersion") != API_SCHEMA:
        raise ValueError("unsupported Local API schema")
    revision = _integer(snapshot.get("dataRevision"), "snapshot revision")
    if _integer(capabilities.get("dataRevision"), "capability revision") != revision:
        raise ValueError("Local API revision changed during export")
    version = product.get("version")
    build = product.get("build")
    if not isinstance(version, str) or VERSION.fullmatch(version) is None:
        raise ValueError("invalid product version")
    if not isinstance(build, str) or not build.isascii() or not build.isdecimal():
        raise ValueError("invalid product build")
    macos = runtime.get("macOS")
    architecture = runtime.get("architecture")
    if not isinstance(macos, str) or len(macos) > 32 or not macos:
        raise ValueError("invalid macOS version")
    if architecture not in {"arm64", "x86_64", "unknown"}:
        raise ValueError("invalid architecture")

    summary = _mapping(snapshot.get("summary"), "summary")
    providers = _items(snapshot.get("providers"), "providers")
    quotas = [_mapping(item, "quota window") for item in _items(snapshot.get("quotaWindows"), "quota windows")]
    sources = [_mapping(item, "source") for item in _items(snapshot.get("sources"), "sources")]
    source_states = [_identifier(item.get("state"), "source state") for item in sources]
    errors = []
    for source in sources:
        error = source.get("errorCode")
        if error is None:
            continue
        errors.append(error if isinstance(error, str) and ERROR_CODE.fullmatch(error) else "UNCLASSIFIED")
    quota_states = [_identifier(item.get("state"), "quota state") for item in quotas]
    quota_quality = [_identifier(item.get("quality"), "quota quality") for item in quotas]
    stale_count = sum(item.get("stale") is True for item in quotas)
    if any(not isinstance(item.get("stale"), bool) for item in quotas):
        raise ValueError("quota stale state must be boolean")
    current = (clock or (lambda: datetime.now(timezone.utc)))()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("clock must be timezone-aware")
    result = {
        "aggregates": {
            "coveredDayCount": _integer(summary.get("coveredDayCount"), "covered days"),
            "modelCount": _integer(summary.get("modelCount"), "model count"),
            "providerInstanceCount": len(providers),
            "quotaQuality": _counted(quota_quality),
            "quotaStates": _counted(quota_states),
            "quotaWindowCount": len(quotas),
            "staleQuotaWindowCount": stale_count,
            "sourceCount": len(sources),
            "sourceErrorCodes": _counted(errors),
            "sourceStates": _counted(source_states),
            "todayTokens": _integer(summary.get("todayTokens"), "today tokens"),
        },
        "capabilityDeclarations": _capability_declarations(capabilities),
        "exportedAt": current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "localAPI": {
            "catalogRevision": _identifier(snapshot.get("catalogRevision"), "catalog revision"),
            "dataRevision": revision,
            "schemaVersion": API_SCHEMA,
        },
        "product": {"build": build, "version": version},
        "runtime": {"architecture": architecture, "macOS": macos},
        "schemaVersion": DIAGNOSTIC_SCHEMA,
    }
    _validate_export(result, home=Path.home())
    return result


def build_reconciliation_diagnostics(
    snapshot: dict[str, Any],
    capabilities: dict[str, Any],
    activity: dict[str, Any],
    source_status: dict[str, Any],
    *,
    from_day: date,
    to_day: date,
    local_timezone: str,
    product: dict[str, str],
    runtime: dict[str, str],
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Build a redacted, source-aware daily token reconciliation."""
    if (
        not isinstance(from_day, date)
        or isinstance(from_day, datetime)
        or not isinstance(to_day, date)
        or isinstance(to_day, datetime)
        or from_day > to_day
        or (to_day - from_day).days + 1 > MAX_ACTIVITY_RANGE_DAYS
    ):
        raise ValueError("diagnostic date range is invalid or exceeds maximum")
    if not isinstance(local_timezone, str) or not 1 <= len(local_timezone) <= 64:
        raise ValueError("invalid local timezone")
    _timezone_info(local_timezone)

    activity = _mapping(activity, "activity")
    source_status = _mapping(source_status, "source status")
    _reject_sensitive_input(activity)
    _reject_sensitive_input(source_status)
    base = build_diagnostics(
        snapshot,
        capabilities,
        product=product,
        runtime=runtime,
        clock=clock,
    )
    revision = base["localAPI"]["dataRevision"]
    for payload, name in ((activity, "activity"), (source_status, "source status")):
        if payload.get("schemaVersion") != API_SCHEMA:
            raise ValueError(f"unsupported {name} Local API schema")
        if _integer(payload.get("dataRevision"), f"{name} revision") != revision:
            raise ValueError("Local API revision changed during export")

    start_text = from_day.isoformat()
    end_text = to_day.isoformat()
    coverage_by_scope: dict[tuple[str, str, str | None], bool] = {}
    coverage_rows: list[dict[str, Any]] = []
    for raw in _items(activity.get("coverage"), "activity coverage"):
        item = _mapping(raw, "activity coverage row")
        day = _day_text(item.get("day"), "coverage day")
        if not start_text <= day <= end_text:
            raise ValueError("coverage day is outside requested range")
        provider_id = _identifier(item.get("providerId"), "coverage providerId")
        account_ref = _optional_identifier(item.get("accountRef"), "coverage accountRef")
        covered = item.get("covered")
        if not isinstance(covered, bool):
            raise ValueError("coverage must be boolean")
        source_id = _optional_identifier(item.get("sourceId"), "coverage sourceId")
        key = (day, provider_id, account_ref)
        if key in coverage_by_scope:
            raise ValueError("duplicate activity coverage scope")
        coverage_by_scope[key] = covered
        coverage_rows.append({
            "day": day,
            "providerId": provider_id,
            "accountRef": account_ref,
            "covered": covered,
            "sourceId": source_id,
        })
    account_pseudonyms = {
        account_ref: f"account-{index}"
        for index, account_ref in enumerate(
            sorted({key[2] for key in coverage_by_scope if key[2] is not None}),
            start=1,
        )
    }

    token_conventions = {
        "input_includes_cache", "components_disjoint", "provider_reported", "unknown"
    }
    daily_usage: list[dict[str, Any]] = []
    for raw in _items(activity.get("rows"), "activity rows"):
        item = _mapping(raw, "activity row")
        day = _day_text(item.get("day"), "activity day")
        if not start_text <= day <= end_text:
            raise ValueError("activity day is outside requested range")
        provider_id = _identifier(item.get("providerId"), "activity providerId")
        account_ref = _optional_identifier(item.get("accountRef"), "activity accountRef")
        coverage_key = (day, provider_id, account_ref)
        if coverage_key not in coverage_by_scope:
            raise ValueError("activity row has no coverage declaration")
        convention = item.get("tokenCountingConvention")
        if convention not in token_conventions:
            raise ValueError("activity row has an invalid token counting convention")
        reasoning = item.get("reasoningTokens")
        if reasoning is not None:
            reasoning = _integer(reasoning, "reasoning tokens")
        total_tokens = _integer(item.get("totalTokens"), "total tokens")
        input_tokens = _integer(item.get("inputTokens"), "input tokens")
        output_tokens = _integer(item.get("outputTokens"), "output tokens")
        cache_read_tokens = _integer(
            item.get("cacheReadTokens"), "cache read tokens"
        )
        cache_creation_tokens = _integer(
            item.get("cacheCreationTokens"), "cache creation tokens"
        )
        covered = coverage_by_scope[coverage_key]
        daily_usage.append({
            "day": day,
            "providerId": provider_id,
            "accountRef": account_ref,
            "modelId": _identifier(item.get("modelId"), "activity modelId"),
            "totalTokens": total_tokens,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "cacheReadTokens": cache_read_tokens,
            "cacheCreationTokens": cache_creation_tokens,
            "reasoningTokens": reasoning,
            "tokenCountingConvention": convention,
            "sourceId": _identifier(item.get("sourceId"), "activity sourceId"),
            "quality": _identifier(item.get("quality"), "activity quality"),
            "coverage": "covered" if covered else "missing",
            "importedAt": _timestamp(item.get("importedAt"), "activity importedAt"),
            "completeness": (
                "missing" if not covered else "partial" if reasoning is None else "complete"
            ),
            "reconciliation": _token_reconciliation(
                convention=convention,
                total_tokens=total_tokens,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
                reasoning_tokens=reasoning,
            ),
        })
    daily_usage.sort(key=lambda item: (
        item["day"], item["providerId"], item["accountRef"] or "",
        item["modelId"], item["sourceId"],
    ))

    issues: list[dict[str, Any]] = []
    for item in coverage_rows:
        if not item["covered"]:
            issues.append({
                "code": "coverage_gap",
                "day": item["day"],
                "providerId": item["providerId"],
                "accountRef": item["accountRef"],
                "sourceId": item["sourceId"],
            })

    effective_rows: dict[tuple[str, str, str | None, str], list[dict[str, Any]]] = {}
    for row in daily_usage:
        key = (
            row["day"], row["providerId"], row["accountRef"], row["modelId"]
        )
        effective_rows.setdefault(key, []).append(row)
    for (day, provider_id, account_ref, model_id), rows in effective_rows.items():
        if len(rows) < 2:
            continue
        source_ids = sorted({row["sourceId"] for row in rows})
        evidence = {
            "day": day,
            "providerId": provider_id,
            "accountRef": account_ref,
            "modelId": model_id,
            "rowCount": len(rows),
            "sourceIds": source_ids,
        }
        issues.append({"code": "duplicate_effective_row", **evidence})
        if len(source_ids) > 1:
            issues.append({"code": "duplicate_source_candidate", **evidence})

    source_states: list[str] = []
    source_errors: list[str] = []
    for raw in _items(source_status.get("sources"), "source statuses", limit=1_000):
        item = _mapping(raw, "source status")
        provider_id = _identifier(item.get("providerId"), "source providerId")
        source_id = _identifier(item.get("sourceId"), "source sourceId")
        state = _identifier(item.get("state"), "source state")
        last_attempt = _timestamp(item.get("lastAttemptAt"), "source lastAttemptAt")
        last_success = _timestamp(
            item.get("lastSuccessAt"), "source lastSuccessAt", optional=True
        )
        stale_at = _timestamp(item.get("staleAt"), "source staleAt", optional=True)
        error_code = _error_code(item.get("errorCode"))
        source_states.append(state)
        if error_code is not None:
            source_errors.append(error_code)
        evidence = {
            "providerId": provider_id,
            "sourceId": source_id,
            "state": state,
            "lastAttemptAt": last_attempt,
            "lastSuccessAt": last_success,
            "staleAt": stale_at,
            "errorCode": error_code,
        }
        if state == "stale":
            issues.append({"code": "source_stale", **evidence})
            retained = [
                row for row in daily_usage
                if row["providerId"] == provider_id and row["sourceId"] == source_id
            ]
            if last_success is not None and retained:
                issues.append({
                    "code": "last_good_retained",
                    **evidence,
                    "retainedRowCount": len(retained),
                    "retainedFrom": min(row["day"] for row in retained),
                    "retainedTo": max(row["day"] for row in retained),
                })
        elif state != "ok" and error_code is not None:
            issues.append({"code": "source_error", **evidence})

    issues.sort(key=lambda item: (
        item["code"], item.get("day", ""), item.get("providerId", ""),
        item.get("accountRef") or "", item.get("modelId", ""),
        item.get("sourceId") or "",
    ))
    fully_covered = bool(coverage_rows) and all(
        item["covered"] for item in coverage_rows
    )
    has_tokens = bool(daily_usage)

    if not has_tokens:
        aggregate_completeness = "covered_zero" if fully_covered else "missing"
    elif fully_covered and all(
        item["completeness"] == "complete" for item in daily_usage
    ):
        aggregate_completeness = "complete"
    else:
        aggregate_completeness = "partial"

    def observed_component_total(name: str) -> int | None:
        if not has_tokens:
            return 0 if aggregate_completeness == "covered_zero" else None
        values = [row[name] for row in daily_usage]
        if any(value is None for value in values):
            return None
        return sum(values)

    component_names = (
        "totalTokens", "inputTokens", "outputTokens", "cacheReadTokens",
        "cacheCreationTokens", "reasoningTokens",
    )
    observed_token_totals = {
        name: observed_component_total(name) for name in component_names
    }
    if aggregate_completeness in {"complete", "covered_zero"}:
        token_totals = dict(observed_token_totals)
    else:
        token_totals = {name: None for name in component_names}

    def pseudonymized_account_ref(value: str | None) -> str | None:
        if value is None:
            return None
        return account_pseudonyms[value]

    exported_daily_usage = [
        {**item, "accountRef": pseudonymized_account_ref(item["accountRef"])}
        for item in daily_usage
    ]
    exported_issues = [
        {
            **item,
            **(
                {"accountRef": pseudonymized_account_ref(item["accountRef"])}
                if "accountRef" in item
                else {}
            ),
        }
        for item in issues
    ]

    issue_counts = _counted([item["code"] for item in issues])
    result = {
        "aggregates": {
            "aggregateCompleteness": aggregate_completeness,
            "completenessCounts": _counted([
                item["completeness"] for item in daily_usage
            ]),
            "countingConventionCounts": _counted([
                item["tokenCountingConvention"] for item in daily_usage
            ]),
            "coverageGapCount": sum(not item["covered"] for item in coverage_rows),
            "coverageRecordCount": len(coverage_rows),
            "coveredScopeDayCount": sum(item["covered"] for item in coverage_rows),
            "dailyUsageRowCount": len(daily_usage),
            "issueCounts": issue_counts,
            "modelCount": len({item["modelId"] for item in daily_usage}),
            "providerCount": len({item["providerId"] for item in daily_usage}),
            "qualityCounts": _counted([item["quality"] for item in daily_usage]),
            "reconciliationStatusCounts": _counted([
                item["reconciliation"]["status"] for item in daily_usage
            ]),
            "sourceCount": len(source_states),
            "sourceErrorCodes": _counted(source_errors),
            "sourceStates": _counted(source_states),
            "observedTokenTotals": observed_token_totals,
            "tokenTotals": token_totals,
        },
        "capabilityDeclarations": base["capabilityDeclarations"],
        "dailyUsage": exported_daily_usage,
        "exportedAt": base["exportedAt"],
        "issues": exported_issues,
        "localAPI": {
            **base["localAPI"],
            "generatedAt": _timestamp(snapshot.get("generatedAt"), "snapshot generatedAt"),
        },
        "observability": {
            "accountReferences": "per_export_pseudonyms",
            "sourceSelectionHistory": "not_observable",
        },
        "product": base["product"],
        "range": {
            "from": start_text,
            "to": end_text,
            "timezone": local_timezone,
        },
        "runtime": base["runtime"],
        "schemaVersion": RECONCILIATION_SCHEMA,
    }
    _validate_reconciliation_export(result, home=Path.home())
    return result


def _get(socket_path: Path, route: str) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(3)
    try:
        client.connect(str(socket_path))
        client.sendall(
            f"GET {route} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
        )
        chunks: list[bytes] = []
        size = 0
        while chunk := client.recv(65_536):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("Local API response is too large")
            chunks.append(chunk)
    finally:
        client.close()
    response = b"".join(chunks)
    head, separator, body = response.partition(b"\r\n\r\n")
    if not separator or not head.startswith(b"HTTP/1.1 200 "):
        raise ValueError("Local API route is unavailable")
    payload = json.loads(body)
    return _mapping(payload, "Local API response")


def _product(app: Path) -> dict[str, str]:
    info = app / "Contents/Info.plist"
    try:
        with info.open("rb") as handle:
            payload = plistlib.load(handle)
        return {
            "version": str(payload["CFBundleShortVersionString"]),
            "build": str(payload["CFBundleVersion"]),
        }
    except (OSError, KeyError, ValueError, plistlib.InvalidFileException) as error:
        raise ValueError("installed app metadata is unavailable") from error


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    if not path.is_absolute() or not path.parent.is_dir():
        raise ValueError("output must be an absolute path in an existing directory")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Export redacted OpenUsage Bar diagnostics.")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--socket", type=Path,
        default=Path.home() / ".local/state/openusage-bar/openusage.sock",
    )
    parser.add_argument("--app", type=Path, default=Path("/Applications/OpenUsage Bar.app"))
    parser.add_argument(
        "--schema-version", choices=("1", "2"), default="1",
        help=(
            "diagnostic schema to export (default: 1; choose 2 for daily "
            "token reconciliation)"
        ),
    )
    parser.add_argument("--from", dest="from_day", type=date.fromisoformat)
    parser.add_argument("--to", dest="to_day", type=date.fromisoformat)
    parser.add_argument("--timezone", default=_local_timezone_name())
    arguments = parser.parse_args()
    if not arguments.socket.is_absolute() or "\x00" in str(arguments.socket):
        parser.error("socket path must be absolute")
    if (arguments.from_day is None) != (arguments.to_day is None):
        parser.error("--from and --to must be provided together")
    try:
        local_timezone = _timezone_info(arguments.timezone)
        from_day = arguments.from_day
        to_day = arguments.to_day
        if arguments.schema_version == "2" and from_day is None:
            local_today = datetime.now(timezone.utc).astimezone(local_timezone).date()
            from_day = local_today
            to_day = local_today
        diagnostic = None
        for _ in range(2):
            app = arguments.app
            default_app = Path("/Applications/OpenUsage Bar.app")
            home_app = Path.home() / "Applications/OpenUsage Bar.app"
            if app == default_app and not app.is_dir() and home_app.is_dir():
                app = home_app
            snapshot = _get(arguments.socket, "/v1/snapshot")
            capabilities = _get(arguments.socket, "/v1/capabilities")
            try:
                product = _product(app)
                runtime = {
                    "macOS": platform.mac_ver()[0] or "unknown",
                    "architecture": platform.machine()
                    if platform.machine() in {"arm64", "x86_64"}
                    else "unknown",
                }
                if arguments.schema_version == "1":
                    diagnostic = build_diagnostics(
                        snapshot, capabilities, product=product, runtime=runtime
                    )
                else:
                    assert from_day is not None and to_day is not None
                    activity = _get(
                        arguments.socket,
                        f"/v1/activity/daily?from={from_day.isoformat()}&to={to_day.isoformat()}",
                    )
                    source_status = _get(arguments.socket, "/v1/sources/status")
                    diagnostic = build_reconciliation_diagnostics(
                        snapshot,
                        capabilities,
                        activity,
                        source_status,
                        from_day=from_day,
                        to_day=to_day,
                        local_timezone=arguments.timezone,
                        product=product,
                        runtime=runtime,
                    )
                break
            except ValueError as error:
                if "revision changed" not in str(error):
                    raise
        if diagnostic is None:
            raise ValueError("Local API revision did not stabilize")
        _write_private(arguments.output, diagnostic)
    except (OSError, ValueError, json.JSONDecodeError):
        print("diagnostics export unavailable", file=os.sys.stderr)
        return 1
    print("diagnostics_exported=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
