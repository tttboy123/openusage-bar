#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "openusage_bar/resources/local-api-v1.schema.json"


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
    source = closed(
        {
            "providerId": {"type": "string"}, "sourceId": {"type": "string"},
            "state": {"type": "string"}, "lastAttemptAt": {"type": "string", "format": "date-time"},
            "lastSuccessAt": nullable("string"), "staleAt": nullable("string"),
            "errorCode": nullable("string"),
        },
        ["providerId", "sourceId", "state", "lastAttemptAt", "lastSuccessAt", "staleAt", "errorCode"],
    )
    snapshot = envelope(
        {
            "localDay": {"type": "string", "format": "date"},
            "summary": closed(
                {
                    "todayTokens": {"type": "integer", "minimum": 0},
                    "modelCount": {"type": "integer", "minimum": 0},
                    "coveredDayCount": {"type": "integer", "minimum": 0},
                },
                ["todayTokens", "modelCount", "coveredDayCount"],
            ),
            "quotaWindows": {"type": "array", "items": quota},
            "providers": {"type": "array", "items": provider},
            "sources": {"type": "array", "items": source},
            "catalogRevision": {"type": "string"},
        },
        ["localDay", "summary", "quotaWindows", "providers", "sources", "catalogRevision"],
    )
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
        "oneOf": [snapshot, changes, error],
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
