"""Read-only CC Switch adapters for the UsageHub ledger.

CC Switch persists its provider state and per-day cost rollups in
``~/.cc-switch/cc-switch.db``. These adapters only read that database
(``mode=ro``, ``query_only=ON``) and never touch ``settings_config`` values,
which may contain credentials.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from .activity_store import DailyCostRow
from .models import Category, ProviderCard, ProviderStatus
from .providers.contracts import (
    CostImportResult,
    CostImportSuccess,
    ImportFailure,
)


CC_SWITCH_COST_SOURCE_ID = "cc_switch.rollups"
CC_SWITCH_STATUS_SOURCE_ID = "cc_switch.status"
DEFAULT_CC_SWITCH_DB = Path.home() / ".cc-switch" / "cc-switch.db"


def _read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


class CcSwitchCostImporter:
    """Import CC Switch daily cost rollups into the monetary ledger."""

    usage_source_id = None
    cost_source_id = CC_SWITCH_COST_SOURCE_ID
    performance_source_class = "local_file"

    def __init__(
        self,
        *,
        db_path: Path | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_CC_SWITCH_DB
        self.account_ref = "cc_switch"

    def fetch_costs(self, since: date, until: date) -> CostImportResult:
        if since > until:
            return ImportFailure("invalid_request")
        if not self.db_path.is_file():
            return ImportFailure("source_unavailable")
        try:
            connection = _read_only_connection(self.db_path)
        except (OSError, sqlite3.Error):
            return ImportFailure("source_unavailable")
        try:
            rows = self._query(connection, since, until)
            return CostImportSuccess(since, until, rows)
        except (sqlite3.Error, InvalidOperation, ValueError):
            return ImportFailure("import_failed")
        finally:
            connection.close()

    def _query(
        self,
        connection: sqlite3.Connection,
        since: date,
        until: date,
    ) -> tuple[DailyCostRow, ...]:
        sql = (
            "SELECT date, app_type, provider_id, model, total_cost_usd "
            "FROM usage_daily_rollups "
            "WHERE date >= ? AND date <= ? "
            "ORDER BY date, app_type, provider_id, model"
        )
        aggregated: dict[str, Decimal] = {}
        order: list[str] = []
        for raw in connection.execute(sql, (since.isoformat(), until.isoformat())):
            day, app_type, upstream, _model, total = raw
            if total is None:
                amount = Decimal(0)
            else:
                try:
                    amount = Decimal(str(total))
                except InvalidOperation:
                    amount = Decimal(0)
            if day not in aggregated:
                order.append(day)
                aggregated[day] = Decimal(0)
            aggregated[day] += amount
        rows: list[DailyCostRow] = []
        for day in order:
            rows.append(
                DailyCostRow(
                    day=day,
                    provider_id="cc_switch",
                    cost_kind="actual",
                    currency="USD",
                    amount=format(aggregated[day], "f"),
                    basis="cc_switch.rollups",
                    quality="upstream_declared",
                    account_ref=self.account_ref,
                )
            )
        return tuple(rows)


class CcSwitchStatusAdapter:
    """Publish the CC Switch active Codex provider as a local fact card."""

    source_id = CC_SWITCH_STATUS_SOURCE_ID
    source_priority = 10
    performance_source_class = "local_file"

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_CC_SWITCH_DB
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(self) -> ProviderCard:
        now = self.clock()
        active = self._active_codex_provider()
        if active is None:
            return ProviderCard(
                provider_id="cc_switch",
                name="CC Switch",
                category=Category.LOCAL,
                status=ProviderStatus.UNKNOWN,
                primary=None,
                detail="CC Switch 数据库不可用",
                source="CC Switch",
                refreshed_at=now,
                remaining_percent=None,
                resets_at=None,
                family_id="cc_switch",
                credential_source="local_config",
                source_kind="local_file",
            )
        return ProviderCard(
            provider_id="cc_switch",
            name="CC Switch",
            category=Category.LOCAL,
            status=ProviderStatus.OK,
            primary=active,
            detail=f"Codex 当前供应商: {active}",
            source="CC Switch",
            refreshed_at=now,
            remaining_percent=None,
            resets_at=None,
            family_id="cc_switch",
            credential_source="local_config",
            source_kind="local_file",
        )

    def _active_codex_provider(self) -> str | None:
        if not self.db_path.is_file():
            return None
        try:
            connection = _read_only_connection(self.db_path)
        except (OSError, sqlite3.Error):
            return None
        try:
            row = connection.execute(
                "SELECT name FROM providers "
                "WHERE app_type = 'codex' AND is_current = 1 "
                "ORDER BY sort_index LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            connection.close()
        if row is None or not isinstance(row[0], str) or not row[0]:
            return None
        return row[0]
