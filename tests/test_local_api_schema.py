from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path

from openusage_bar.activity_store import ActivityStore
from openusage_bar.query import QueryService, to_wire
from scripts.generate_local_api_schema import render_schema


ROOT = Path(__file__).parents[1]
SCHEMA = ROOT / "openusage_bar/resources/local-api-v1.schema.json"
NOW = datetime(2026, 7, 18, 1, 0, tzinfo=timezone.utc)


def validate_snapshot(payload: dict[str, object]) -> None:
    required = {
        "schemaVersion", "dataRevision", "generatedAt", "localDay", "summary",
        "quotaWindows", "providers", "sources", "catalogRevision",
    }
    if set(payload) != required or payload.get("schemaVersion") != "1.0":
        raise ValueError("invalid snapshot envelope")
    if isinstance(payload.get("dataRevision"), bool) or not isinstance(payload.get("dataRevision"), int):
        raise ValueError("invalid revision")
    forbidden = ("secret", "password", "cookie", "token", "authorization")
    for value in (payload, *payload["quotaWindows"], *payload["providers"], *payload["sources"]):
        if any(any(term in key.lower() for term in forbidden) for key in value):
            raise ValueError("private field")
    for window in payload["quotaWindows"]:
        if window["state"] == "unknown" and any(
            window[name] is not None
            for name in ("used", "quotaLimit", "remaining", "remainingRatio")
        ):
            raise ValueError("unknown quota has a value")


def validate_changes(payload: dict[str, object]) -> None:
    required = {"schemaVersion", "dataRevision", "generatedAt", "records", "nextCursor", "hasMore"}
    if set(payload) != required or not isinstance(payload.get("nextCursor"), int):
        raise ValueError("invalid changes cursor")


class LocalAPISchemaTests(unittest.TestCase):
    def test_generated_schema_is_current_and_draft_2020_12(self):
        self.assertEqual(json.loads(SCHEMA.read_text()), render_schema())
        self.assertEqual(render_schema()["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_snapshot_contract_rejects_missing_revision_private_fields_and_unknown_values(self):
        store = ActivityStore(":memory:")
        try:
            fixture = to_wire(QueryService(store, clock=lambda: NOW).resource_snapshot(date(2026, 7, 18)))
        finally:
            store.close()
        validate_snapshot(fixture)
        for mutation in ("missing_schema", "bool_revision", "private", "unknown_value"):
            changed = deepcopy(fixture)
            if mutation == "missing_schema":
                changed.pop("schemaVersion")
            elif mutation == "bool_revision":
                changed["dataRevision"] = True
            elif mutation == "private":
                changed["apiToken"] = "redacted"
            else:
                changed["quotaWindows"] = [{"state": "unknown", "used": None, "quotaLimit": None, "remaining": None, "remainingRatio": 0}]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_snapshot(changed)

    def test_changes_require_cursor_and_accept_unknown_record_types(self):
        fixture = {
            "schemaVersion": "1.0", "dataRevision": 9,
            "generatedAt": "2026-07-18T01:00:00Z",
            "records": [{"recordType": "future_fact"}],
            "nextCursor": 9, "hasMore": False,
        }
        validate_changes(fixture)
        changed = dict(fixture)
        changed.pop("nextCursor")
        with self.assertRaises(ValueError):
            validate_changes(changed)


if __name__ == "__main__":
    unittest.main()
