"""Read-only OmniRoute adapter for the UsageHub ledger.

OmniRoute persists usage and cost facts under its local data directory
(``~/.omniroute/usage.json`` by default). The parser is deliberately defensive:
missing data is reported as an unavailable source and malformed payloads fail
closed; no credential or request payload is ever read.
"""

from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .activity_store import DailyCostRow
from .providers.contracts import (
    CostImportResult,
    CostImportSuccess,
    ImportFailure,
)


OMNIROUTE_COST_SOURCE_ID = "omniroute.usage"
DEFAULT_OMNIROUTE_USAGE = Path.home() / ".omniroute" / "usage.json"


def _default_usage_path() -> Path:
    configured = os.environ.get("OMNIROUTE_DATA_DIR")
    if configured:
        return Path(configured) / "usage.json"
    return DEFAULT_OMNIROUTE_USAGE


class OmniRouteCostImporter:
    """Import OmniRoute cost facts into the monetary ledger."""

    usage_source_id = None
    cost_source_id = OMNIROUTE_COST_SOURCE_ID
    performance_source_class = "local_file"

    def __init__(self, *, usage_path: Path | None = None) -> None:
        self.usage_path = (
            Path(usage_path) if usage_path is not None else _default_usage_path()
        )
        self.account_ref = "omniroute"

    def fetch_costs(self, since: date, until: date) -> CostImportResult:
        if since > until:
            return ImportFailure("invalid_request")
        if not self.usage_path.is_file():
            return ImportFailure("source_unavailable")
        try:
            payload = json.loads(self.usage_path.read_text(encoding="utf-8"))
            records = _cost_records(payload)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return ImportFailure("import_failed")
        rows: list[DailyCostRow] = []
        try:
            for record in records:
                day, amount = _record_fields(record)
                if day is None or amount is None:
                    continue
                if not (since <= day <= until):
                    continue
                rows.append(
                    DailyCostRow(
                        day=day.isoformat(),
                        provider_id="omniroute",
                        cost_kind="actual",
                        currency="USD",
                        amount=format(Decimal(str(amount)), "f"),
                        basis="omniroute_usage",
                        quality="unverified",
                        account_ref=self.account_ref,
                    )
                )
        except (InvalidOperation, ValueError):
            return ImportFailure("import_failed")
        return CostImportSuccess(since, until, tuple(rows))


def _cost_records(payload: object) -> list[object]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("usage", "rows", "records", "costs"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _record_fields(record: object) -> tuple[date | None, object | None]:
    if not isinstance(record, dict):
        return None, None
    raw_day = (
        record.get("date")
        or record.get("day")
        or record.get("Date")
    )
    if not isinstance(raw_day, str):
        return None, None
    try:
        day = date.fromisoformat(raw_day)
    except ValueError:
        return None, None
    amount = (
        record.get("total_cost_usd")
        or record.get("cost_usd")
        or record.get("cost")
        or record.get("totalCostUsd")
    )
    if amount is None:
        return None, None
    if isinstance(amount, bool) or not isinstance(amount, (int, float, str)):
        return None, None
    return day, amount
