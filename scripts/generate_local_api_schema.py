#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "openusage_bar/resources/local-api-v1.schema.json"
CATALOG_SOURCE_COUNT = 49
LOCAL_API_ROUTES = [
    "/v1/health",
    "/v1/schema",
    "/v1/schema.json",
    "/v1/summary",
    "/v1/snapshot",
    "/v1/capabilities",
    "/v1/providers",
    "/v1/capacity",
    "/v1/activity/daily",
    "/v1/balances",
    "/v1/costs/daily",
    "/v1/quotas/history",
    "/v1/sources/status",
    "/v1/changes",
    "/v1/quick-connect",
]


def nullable(kind: str) -> dict[str, object]:
    return {"type": [kind, "null"]}


def closed(properties: dict[str, object], required: list[str]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def envelope(properties: dict[str, object], required: list[str]) -> dict[str, object]:
    return closed(
        {
            "schemaVersion": {"const": "1.0"},
            "dataRevision": {"type": "integer", "minimum": 0},
            "generatedAt": {"type": "string", "format": "date-time"},
            **properties,
        },
        ["schemaVersion", "dataRevision", "generatedAt", *required],
    )


def summary_contract(schema: dict[str, object]) -> dict[str, object]:
    schema["allOf"] = [
        {
            "if": {
                "properties": {"todayTokens": {"type": "null"}},
                "required": ["todayTokens"],
            },
            "then": {
                "properties": {
                    "modelCount": {"const": 0},
                    "coveredDayCount": {"const": 0},
                }
            },
            "else": {
                "if": {
                    "properties": {"modelCount": {"const": 0}},
                    "required": ["modelCount"],
                },
                "then": {
                    "properties": {
                        "todayTokens": {"const": 0},
                        "coveredDayCount": {"minimum": 1},
                    }
                },
            },
        }
    ]
    return schema


def render_schema() -> dict[str, object]:
    applies_to = closed(
        {
            "kind": {"enum": ["subscription", "account", "model"]},
            "modelIds": {
                "type": "array", "items": {"type": "string"}, "uniqueItems": True,
            },
        },
        ["kind", "modelIds"],
    )
    applies_to["allOf"] = [
        {
            "if": {"properties": {"kind": {"const": "model"}}, "required": ["kind"]},
            "then": {"properties": {"modelIds": {"minItems": 1}}},
            "else": {"properties": {"modelIds": {"maxItems": 0}}},
        }
    ]
    quota = closed(
        {
            "recordId": {"type": "string"},
            "providerId": {"type": "string"},
            "accountRef": nullable("string"),
            "quotaName": {"type": "string"},
            "unit": {"type": "string"},
            "used": nullable("string"),
            "quotaLimit": nullable("string"),
            "remaining": nullable("string"),
            "remainingRatio": nullable("number"),
            "resetsAt": nullable("string"),
            "periodStart": nullable("string"),
            "periodEnd": nullable("string"),
            "observedAt": {"type": "string", "format": "date-time"},
            "freshnessSeconds": {"type": "integer", "minimum": 0},
            "state": {"type": "string"},
            "quality": {"type": "string"},
            "stale": {"type": "boolean"},
            "revision": {"type": "integer", "minimum": 1},
            "sourceId": {"type": "string"},
            "quotaWindow": {"type": "string"},
            "appliesTo": applies_to,
            "estimatedCostPerMillionTokens": nullable("string"),
            "constraints": {"type": "array", "items": {"type": "string"}},
        },
        [
            "recordId", "providerId", "accountRef", "quotaName", "unit",
            "used", "quotaLimit", "remaining", "remainingRatio", "resetsAt",
            "periodStart", "periodEnd", "observedAt", "freshnessSeconds",
            "state", "quality", "stale", "revision", "sourceId",
            "quotaWindow", "appliesTo",
            "estimatedCostPerMillionTokens", "constraints",
        ],
    )
    quota["allOf"] = [{
        "if": {"properties": {"state": {"const": "unknown"}}, "required": ["state"]},
        "then": {"properties": {
            name: {"type": "null"}
            for name in ("used", "quotaLimit", "remaining", "remainingRatio")
        }},
    }]
    balance = closed(
        {
            "recordId": {"type": "string"},
            "providerId": {"type": "string"},
            "accountRef": nullable("string"),
            "currency": {"type": "string"},
            "available": nullable("string"),
            "voucher": nullable("string"),
            "cash": nullable("string"),
            "observedAt": {"type": "string", "format": "date-time"},
            "freshnessSeconds": {"type": "integer", "minimum": 0},
            "state": {"type": "string"},
            "quality": {"type": "string"},
            "stale": {"type": "boolean"},
            "revision": {"type": "integer", "minimum": 1},
            "sourceId": {"type": "string"},
        },
        [
            "recordId", "providerId", "accountRef", "currency", "available",
            "voucher", "cash", "observedAt", "freshnessSeconds", "state",
            "quality", "stale", "revision", "sourceId",
        ],
    )
    balance["allOf"] = [{
        "if": {
            "properties": {"state": {"const": "unknown"}},
            "required": ["state"],
        },
        "then": {"properties": {
            name: {"type": "null"}
            for name in ("available", "voucher", "cash")
        }},
    }]
    balances = envelope(
        {"balances": {"type": "array", "items": balance}},
        ["balances"],
    )
    provider = closed(
        {
            "providerId": {"type": "string"}, "familyId": {"type": "string"},
            "displayName": {"type": "string"}, "category": {"type": "string"},
            "credentialSource": {"type": "string"}, "sourceKind": {"type": "string"},
            "observedAt": {"type": "string", "format": "date-time"},
            "revision": {"type": "integer", "minimum": 1},
        },
        ["providerId", "familyId", "displayName", "category", "credentialSource", "sourceKind", "observedAt", "revision"],
    )
    provider_or_capacity = envelope(
        {
            "providers": {
                "type": "array",
                "items": {"oneOf": [provider, quota]},
            }
        },
        ["providers"],
    )
    provider_or_capacity["anyOf"] = [
        {"properties": {"providers": {"items": provider}}},
        {"properties": {"providers": {"items": quota}}},
    ]
    source = closed(
        {
            "providerId": {"type": "string"}, "sourceId": {"type": "string"},
            "state": {"type": "string"}, "lastAttemptAt": {"type": "string", "format": "date-time"},
            "lastSuccessAt": nullable("string"), "staleAt": nullable("string"),
            "errorCode": nullable("string"),
        },
        ["providerId", "sourceId", "state", "lastAttemptAt", "lastSuccessAt", "staleAt", "errorCode"],
    )
    summary_properties = {
        "todayTokens": {"type": ["integer", "null"], "minimum": 0},
        "modelCount": {"type": "integer", "minimum": 0},
        "coveredDayCount": {"type": "integer", "minimum": 0},
    }
    summary_required = ["todayTokens", "modelCount", "coveredDayCount"]
    summary = summary_contract(envelope(summary_properties, summary_required))
    quota_hub = {
        "type": "array",
        "items": closed(
            {
                "currency": {"type": "string"},
                "totalAvailable": {"type": "string"},
                "providerCount": {"type": "integer", "minimum": 1},
                "provenance": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 3,
                        "maxItems": 3,
                    },
                },
            },
            ["currency", "totalAvailable", "providerCount", "provenance"],
        ),
    }
    snapshot = envelope(
        {
            "localDay": {"type": "string", "format": "date"},
            "summary": summary_contract(
                closed(dict(summary_properties), list(summary_required))
            ),
            "balances": {"type": "array", "items": balance},
            "quotaWindows": {"type": "array", "items": quota},
            "quotaHub": quota_hub,
            "providers": {"type": "array", "items": provider},
            "sources": {"type": "array", "items": source},
            "catalogRevision": {"type": "string"},
        },
        [
            "localDay", "summary", "quotaWindows", "providers",
            "sources", "catalogRevision",
        ],
    )
    capability_state = {"enum": ["supported", "unsupported", "unknown"]}
    quota_window_capability = closed(
        {
            "state": capability_state,
            "values": {
                "type": "array",
                "items": {
                    "enum": [
                        "session",
                        "five_hour",
                        "weekly",
                        "monthly",
                        "billing_cycle",
                        "model_specific",
                    ]
                },
                "uniqueItems": True,
            },
        },
        ["state", "values"],
    )
    quota_window_capability["oneOf"] = [
        {
            "properties": {
                "state": {"const": "supported"},
                "values": {"minItems": 1},
            }
        },
        {
            "properties": {
                "state": {"const": "unsupported"},
                "values": {"maxItems": 0},
            }
        },
        {
            "properties": {
                "state": {"const": "unknown"},
                "values": {"maxItems": 0},
            }
        },
    ]
    capability_names = [
        "tokenHistory",
        "modelBreakdown",
        "resetTimestamps",
        "billing",
        "credits",
        "balance",
        "cost",
        "rateLimits",
        "serviceStatus",
    ]
    provider_capabilities = closed(
        {
            "quotaWindows": quota_window_capability,
            **{name: capability_state for name in capability_names},
        },
        ["quotaWindows", *capability_names],
    )
    platform_support = closed(
        {
            "state": capability_state,
            "reasonCode": {
                "enum": [
                    "supported_sources_available",
                    "source_level_evidence_unverified",
                    "runtime_platform_unknown",
                ]
            },
        },
        ["state", "reasonCode"],
    )
    platform_support["oneOf"] = [
        {
            "properties": {
                "state": {"const": "supported"},
                "reasonCode": {"const": "supported_sources_available"},
            }
        },
        {
            "properties": {
                "state": {"const": "unsupported"},
                "reasonCode": {
                    "const": "source_level_evidence_unverified"
                },
            }
        },
        {
            "properties": {
                "state": {"const": "unknown"},
                "reasonCode": {"const": "runtime_platform_unknown"},
            }
        },
    ]
    capability_source = closed(
        {
            "sourceId": {"type": "string"},
            "kind": {
                "enum": [
                    "browser_session",
                    "builtin_api",
                    "keychain",
                    "local_database",
                    "local_log",
                    "official_api",
                    "openusage",
                ]
            },
            "timeoutSeconds": {"type": "integer", "minimum": 1},
            "freshnessSeconds": {"type": "integer", "minimum": 0},
            "credentialType": {
                "enum": [
                    "api_key",
                    "browser_session",
                    "keychain",
                    "local",
                    "oauth",
                    "provider_owned",
                ]
            },
            "requiresCredential": {"type": "boolean"},
            "operatingSystems": {
                "type": "array",
                "items": {"enum": ["macos", "windows", "linux"]},
                "minItems": 1,
                "uniqueItems": True,
            },
            "stability": {
                "enum": ["stable", "experimental", "pinned", "opaque"]
            },
            "provenance": {
                "enum": [
                    "openusage_upstream",
                    "openusage_bar_builtin",
                    "provider_official",
                    "provider_local",
                    "user_session",
                ]
            },
            "factFamilies": {
                "type": "array",
                "items": {
                    "enum": [
                        "detection",
                        "token_activity",
                        "subscription_capacity",
                        "api_balance",
                        "api_spend",
                    ]
                },
                "minItems": 1,
                "uniqueItems": True,
            },
            "authority": {
                "enum": [
                    "provider_official",
                    "provider_local",
                    "third_party",
                    "user_supplied",
                    "unknown",
                ]
            },
            "accountScope": {
                "enum": [
                    "local_profile",
                    "configured_account",
                    "organization",
                    "provider",
                    "unknown",
                ]
            },
            "modelScope": {
                "enum": ["per_model", "aggregate", "mixed", "unknown"]
            },
            "verification": {
                "enum": [
                    "live_account",
                    "fixture",
                    "upstream_declared",
                    "unverified",
                ]
            },
            "platformSupport": platform_support,
        },
        [
            "sourceId",
            "kind",
            "timeoutSeconds",
            "freshnessSeconds",
            "credentialType",
            "requiresCredential",
            "operatingSystems",
            "stability",
            "provenance",
            "factFamilies",
            "authority",
            "accountScope",
            "modelScope",
            "verification",
            "platformSupport",
        ],
    )
    capability_provider = closed(
        {
            "providerId": {"type": "string"},
            "familyId": {"type": "string"},
            "displayName": {"type": "string"},
            "category": {"enum": ["api", "local_tool", "subscription"]},
            "metricFamilies": {
                "type": "array",
                "items": {
                    "enum": [
                        "billing",
                        "operational",
                        "subscription_quota",
                        "token_activity",
                    ]
                },
                "minItems": 1,
                "uniqueItems": True,
            },
            "regions": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            },
            "supportsAccounts": {"type": "boolean"},
            "capabilities": provider_capabilities,
            "sources": {
                "type": "array",
                "items": capability_source,
                "minItems": 1,
            },
        },
        [
            "providerId",
            "familyId",
            "displayName",
            "category",
            "metricFamilies",
            "regions",
            "supportsAccounts",
            "capabilities",
            "sources",
        ],
    )
    observer_platform = closed(
        {
            "operatingSystem": {"enum": ["macos", "windows", "linux", None]},
            "support": capability_state,
            "supportedSourceCount": {
                "type": ["integer", "null"],
                "minimum": 0,
                "maximum": CATALOG_SOURCE_COUNT,
            },
            "totalSourceCount": {"const": CATALOG_SOURCE_COUNT},
            "reasonCode": {
                "enum": [
                    "supported_sources_available",
                    "source_level_evidence_unverified",
                    "runtime_platform_unknown",
                ]
            },
        },
        [
            "operatingSystem",
            "support",
            "supportedSourceCount",
            "totalSourceCount",
            "reasonCode",
        ],
    )
    observer_platform["oneOf"] = [
        {
            "properties": {
                "operatingSystem": {"enum": ["macos", "windows", "linux"]},
                "support": {"const": "supported"},
                "supportedSourceCount": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": CATALOG_SOURCE_COUNT,
                },
                "reasonCode": {"const": "supported_sources_available"},
            }
        },
        {
            "properties": {
                "operatingSystem": {"enum": ["macos", "windows", "linux"]},
                "support": {"const": "unsupported"},
                "supportedSourceCount": {"const": 0},
                "reasonCode": {
                    "const": "source_level_evidence_unverified"
                },
            }
        },
        {
            "properties": {
                "operatingSystem": {"const": None},
                "support": {"const": "unknown"},
                "supportedSourceCount": {"const": None},
                "reasonCode": {"const": "runtime_platform_unknown"},
            }
        },
    ]
    capabilities = envelope(
        {
            "upstream": closed(
                {
                    "name": {"const": "openusage"},
                    "version": {"type": "string"},
                    "revision": {"type": "string"},
                    "familyCount": {"type": "integer", "minimum": 0},
                },
                ["name", "version", "revision", "familyCount"],
            ),
            "observerPlatform": observer_platform,
            "providers": {
                "type": "array",
                "items": capability_provider,
            },
        },
        ["upstream", "observerPlatform", "providers"],
    )
    activity_row = closed(
        {
            "day": {"type": "string", "format": "date"},
            "providerId": {"type": "string"},
            "accountRef": nullable("string"),
            "modelId": {"type": "string"},
            "inputTokens": {"type": "integer", "minimum": 0},
            "outputTokens": {"type": "integer", "minimum": 0},
            "cacheReadTokens": {"type": "integer", "minimum": 0},
            "cacheCreationTokens": {"type": "integer", "minimum": 0},
            "reasoningTokens": nullable("integer"),
            "totalTokens": {"type": "integer", "minimum": 0},
            "tokenCountingConvention": {
                "enum": [
                    "input_includes_cache",
                    "components_disjoint",
                    "provider_reported",
                    "unknown",
                ]
            },
            "costAmount": nullable("string"),
            "costCurrency": nullable("string"),
            "costBasis": nullable("string"),
            "quality": {"type": "string"},
            "importedAt": {"type": "string", "format": "date-time"},
            "revision": {"type": "integer", "minimum": 1},
            "recordId": {"type": "string"},
            "sourceId": {"type": "string"},
            "modelFamily": {"type": "string"},
        },
        [
            "day", "providerId", "accountRef", "modelId", "inputTokens",
            "outputTokens", "cacheReadTokens", "cacheCreationTokens",
            "reasoningTokens", "totalTokens", "tokenCountingConvention",
            "costAmount", "costCurrency", "costBasis", "quality",
            "importedAt", "revision", "recordId", "sourceId", "modelFamily",
        ],
    )
    activity_coverage = closed(
        {
            "day": {"type": "string", "format": "date"},
            "providerId": {"type": "string"},
            "accountRef": nullable("string"),
            "covered": {"type": "boolean"},
            "sourceId": nullable("string"),
        },
        ["day", "providerId", "accountRef", "covered", "sourceId"],
    )
    cost_row = closed(
        {
            "day": {"type": "string", "format": "date"},
            "providerId": {"type": "string"},
            "accountRef": nullable("string"),
            "costKind": {"type": "string"},
            "amount": {"type": "string"},
            "currency": {"type": "string"},
            "basis": {"type": "string"},
            "quality": {"type": "string"},
            "importedAt": {"type": "string", "format": "date-time"},
            "revision": {"type": "integer", "minimum": 1},
            "recordId": {"type": "string"},
        },
        [
            "day", "providerId", "accountRef", "costKind", "amount",
            "currency", "basis", "quality", "importedAt", "revision",
            "recordId",
        ],
    )
    cost_coverage = closed(
        {
            "day": {"type": "string", "format": "date"},
            "providerId": {"type": "string"},
            "accountRef": nullable("string"),
            "covered": {"type": "boolean"},
        },
        ["day", "providerId", "accountRef", "covered"],
    )
    activity_or_cost = envelope(
        {
            "rows": {
                "type": "array",
                "items": {"oneOf": [activity_row, cost_row]},
            },
            "coverage": {
                "type": "array",
                "items": {"oneOf": [activity_coverage, cost_coverage]},
            },
        },
        ["rows", "coverage"],
    )
    activity_or_cost["anyOf"] = [
        {
            "properties": {
                "rows": {"items": activity_row},
                "coverage": {"items": activity_coverage},
            }
        },
        {
            "properties": {
                "rows": {"items": cost_row},
                "coverage": {"items": cost_coverage},
            }
        },
    ]
    change = closed(
        {
            "changeSeq": {"type": "integer", "minimum": 1},
            "recordType": {"type": "string"}, "recordId": {"type": "string"},
            "revision": {"type": "integer", "minimum": 1},
            "operation": {"type": "string"},
            "changedAt": {"type": "string", "format": "date-time"},
            "payloadJson": nullable("string"), "payloadHash": {"type": "string"},
        },
        ["changeSeq", "recordType", "recordId", "revision", "operation", "changedAt", "payloadJson", "payloadHash"],
    )
    changes = envelope(
        {
            "records": {"type": "array", "items": change},
            "nextCursor": {"type": "integer", "minimum": 0},
            "hasMore": {"type": "boolean"},
        },
        ["records", "nextCursor", "hasMore"],
    )
    quota_history_item = closed(
        {
            "snapshotId": {"type": "integer", "minimum": 1},
            "recordId": {"type": "string"},
            "observedAt": {"type": "string", "format": "date-time"},
            "providerId": {"type": "string"},
            "accountRef": nullable("string"),
            "quotaName": {"type": "string"},
            "remainingRatio": nullable("number"),
            "state": {"type": "string"},
            "stale": {"type": "boolean"},
            "sourceId": {"type": "string"},
            "quotaWindow": {"type": "string"},
            "appliesTo": applies_to,
        },
        [
            "snapshotId",
            "recordId",
            "observedAt",
            "providerId",
            "accountRef",
            "quotaName",
            "remainingRatio",
            "state",
            "stale",
            "sourceId",
            "quotaWindow",
            "appliesTo",
        ],
    )
    quota_history = envelope(
        {"snapshots": {"type": "array", "items": quota_history_item}},
        ["snapshots"],
    )
    source_status = envelope(
        {"sources": {"type": "array", "items": source}},
        ["sources"],
    )
    health = envelope(
        {
            "sources": {"type": "array", "items": source},
            "health": closed(
                {
                    "ok": {"const": True},
                    "status": {"const": "ok"},
                },
                ["ok", "status"],
            ),
        },
        ["sources", "health"],
    )
    error_shape = closed(
        {
            "error": closed(
                {
                    "code": {"const": "string"},
                    "message": {"const": "string"},
                },
                ["code", "message"],
            )
        },
        ["error"],
    )
    schema_description = envelope(
        {
            "routes": {"const": LOCAL_API_ROUTES},
            "errorShape": error_shape,
        },
        ["routes", "errorShape"],
    )
    schema_document = closed(
        {
            "$schema": {
                "const": "https://json-schema.org/draft/2020-12/schema"
            },
            "$id": {
                "const": "https://openusage.bar/schemas/local-api-v1.schema.json"
            },
            "title": {"const": "OpenUsage Bar Local API v1"},
            "oneOf": {
                "type": "array",
                "items": {"type": "object"},
                "minItems": 14,
                "maxItems": 14,
            },
        },
        ["$schema", "$id", "title", "oneOf"],
    )
    schema_response = envelope(
        {"schema": schema_document},
        ["schema"],
    )
    quick_connect_item = closed(
        {
            "familyId": {"type": "string"},
            "consoleUrl": {"type": "string", "format": "uri"},
            "authModes": {
                "type": "array",
                "items": {"enum": ["api_key", "oauth", "auto_detect"]},
                "minItems": 1,
                "uniqueItems": True,
            },
            "apiKeyUrl": {
                "type": ["string", "null"],
                "format": "uri",
            },
        },
        ["familyId", "consoleUrl", "authModes", "apiKeyUrl"],
    )
    quick_connect = closed(
        {
            "schemaVersion": {"const": "1.0"},
            "providers": {
                "type": "array",
                "items": quick_connect_item,
            },
        },
        ["schemaVersion", "providers"],
    )
    error = closed(
        {"error": closed(
            {"code": {"type": "string"}, "message": {"type": "string"}},
            ["code", "message"],
        )},
        ["error"],
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://openusage.bar/schemas/local-api-v1.schema.json",
        "title": "OpenUsage Bar Local API v1",
        "oneOf": [
            summary,
            snapshot,
            balances,
            activity_or_cost,
            changes,
            provider_or_capacity,
            capabilities,
            quota_history,
            source_status,
            health,
            schema_description,
            schema_response,
            quick_connect,
            error,
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(render_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
