"""Bounded read-only aggregation of local OpenCode token facts.

OpenCode (opencode.ai) stores every assistant message in a local SQLite
database at ``~/.local/share/opencode/opencode.db``. Each assistant message
records provider/model attribution plus a provider-reported token and cost
breakdown. This importer reads that database strictly read-only and reduces
it to the same per-day, per-model ``DailyUsageRow`` contract the Codex and
Claude Code local importers use. It never writes to the OpenCode database and
never reads prompt or response content.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from .activity_store import DailyUsageRow
from .model_ids import InvalidModelID, canonical_model_id
from .providers.contracts import ImportFailure, UsageImportSuccess


DEFAULT_OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
MAX_OPENCODE_ROWS = 500_000
MAX_TOKENS_PER_MESSAGE = 2**63 - 1


class OpenCodeLocalDailyImporter:
    """Aggregate public Token and cost facts from the local OpenCode ledger."""

    usage_source_id = "opencode.local_sessions"
    history_contract_revision = 1
    cost_source_id = None
    account_ref = ""
    eager_local = True

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        local_timezone=None,
        clock=None,
    ) -> None:
        home = Path.home()
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_OPENCODE_DB
        if not self.db_path.is_absolute():
            raise ValueError("OpenCode database path must be absolute")
        self.local_timezone = (
            local_timezone or datetime.now().astimezone().tzinfo or timezone.utc
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    @staticmethod
    def _valid_range(since: date, until: date) -> bool:
        return (
            isinstance(since, date)
            and not isinstance(since, datetime)
            and isinstance(until, date)
            and not isinstance(until, datetime)
            and since <= until
        )

    def _messages(self) -> Iterable[dict]:
        if not self.db_path.exists():
            raise FileNotFoundError("OpenCode database unavailable")
        try:
            connection = sqlite3.connect(
                f"file:{self.db_path}?mode=ro",
                uri=True,
                timeout=2.0,
            )
        except (OSError, sqlite3.Error):
            raise OSError("OpenCode database unavailable") from None
        try:
            rows = connection.execute(
                "SELECT time_created, data FROM message "
                "WHERE json_extract(data, '$.tokens') IS NOT NULL "
                "LIMIT ?",
                (MAX_OPENCODE_ROWS,),
            )
            for time_created, raw in rows:
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                tokens = payload.get("tokens")
                if not isinstance(tokens, dict) or not tokens:
                    continue
                created = payload.get("time", {}).get("created")
                timestamp = created if isinstance(created, int) else time_created
                if not isinstance(timestamp, int) or timestamp <= 0:
                    continue
                yield {
                    "timestamp": timestamp,
                    "provider_id": payload.get("providerID"),
                    "model_id": payload.get("modelID"),
                    "tokens": tokens,
                    "cost": payload.get("cost"),
                }
        finally:
            connection.close()

    def fetch_usage(self, since: date, until: date):
        if not self._valid_range(since, until):
            return ImportFailure("invalid_request")
        if not self._lock.acquire(blocking=False):
            return ImportFailure("import_in_progress")
        try:
            imported_at = self.clock().astimezone(timezone.utc).isoformat()
            totals: dict[tuple[str, str], dict] = {}
            try:
                messages = list(self._messages())
            except FileNotFoundError:
                return ImportFailure("sessions_unavailable")
            except (OSError, sqlite3.Error, ValueError, TypeError):
                return ImportFailure("sessions_invalid")
            for message in messages:
                timestamp = message["timestamp"]
                try:
                    day = datetime.fromtimestamp(
                        timestamp / 1000, tz=self.local_timezone
                    ).date()
                except (OverflowError, OSError, ValueError):
                    continue
                if not (since <= day <= until):
                    continue
                provider_id = message["provider_id"]
                model_id = message["model_id"]
                if not isinstance(provider_id, str) or not provider_id:
                    provider_id = "opencode"
                try:
                    model = canonical_model_id(model_id or "unknown")
                except InvalidModelID:
                    continue
                tokens = message["tokens"]
                try:
                    entry = totals.setdefault(
                        (day.isoformat(), model),
                        {
                            "input": 0,
                            "output": 0,
                            "cache_read": 0,
                            "cache_creation": 0,
                            "reasoning": 0,
                            "total": 0,
                            "cost": 0.0,
                            "provider_id": provider_id,
                        },
                    )
                    entry["input"] += self._token(tokens, "input")
                    entry["output"] += self._token(tokens, "output")
                    cache = tokens.get("cache")
                    if isinstance(cache, dict):
                        entry["cache_read"] += self._token(cache, "read")
                        entry["cache_creation"] += self._token(cache, "write")
                    entry["reasoning"] += self._token(tokens, "reasoning")
                    entry["total"] += self._token(tokens, "total")
                    cost = message["cost"]
                    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                        entry["cost"] += float(cost)
                except (TypeError, ValueError):
                    continue
            rows = tuple(
                DailyUsageRow(
                    day=day,
                    provider_id="opencode",
                    account_ref="",
                    model_id=model,
                    input_tokens=entry["input"],
                    output_tokens=entry["output"],
                    cache_read_tokens=entry["cache_read"],
                    cache_creation_tokens=entry["cache_creation"],
                    reasoning_tokens=entry["reasoning"],
                    total_tokens=entry["total"],
                    cost_amount=(
                        f"{entry['cost']:.10f}".rstrip("0").rstrip(".")
                        if entry["cost"]
                        else None
                    ),
                    cost_currency="USD" if entry["cost"] else None,
                    cost_basis="provider_reported" if entry["cost"] else None,
                    quality="direct",
                    imported_at=imported_at,
                    token_counting_convention=(
                        "input_includes_cache"
                        if entry["total"]
                        == entry["input"] + entry["output"]
                        else "provider_reported"
                    ),
                )
                for (day, model), entry in sorted(totals.items())
            )
            return UsageImportSuccess(since, until, rows)
        finally:
            self._lock.release()

    @staticmethod
    def _token(tokens: dict, name: str) -> int:
        value = tokens.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > MAX_TOKENS_PER_MESSAGE
        ):
            raise ValueError("invalid token fact")
        return value
