"""Three-source reconciliation for the UsageHub ledger.

Compares Codex local session token facts against CC Switch daily cost
rollups and OmniRoute usage costs for the same calendar range. Discrepancies
are reported as notes, never silently reconciled; missing sources keep their
``None`` cells so a covered zero is never confused with unknown data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from .cc_switch import CC_SWITCH_COST_SOURCE_ID, CcSwitchCostImporter
from .codex_daily import CodexLocalDailyImporter
from .omniroute import OMNIROUTE_COST_SOURCE_ID, OmniRouteCostImporter
from .providers.contracts import (
    CostImportResult,
    ImportFailure,
    UsageImportResult,
)


CODEX_SESSION_SOURCE_ID = "codex.local_sessions"


@dataclass(frozen=True)
class ReconciliationDayRow:
    day: str
    codex_token_total: int | None
    cc_switch_cost_usd: str | None
    omniroute_cost_usd: str | None
    cc_switch_covered: bool
    omniroute_covered: bool
    notes: tuple[str, ...]


@dataclass(frozen=True)
class ReconciliationReport:
    since: date
    until: date
    rows: tuple[ReconciliationDayRow, ...]
    source_statuses: tuple[tuple[str, str, str], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))
        object.__setattr__(self, "source_statuses", tuple(self.source_statuses))


def _token_totals(result: UsageImportResult) -> dict[date, int]:
    totals: dict[date, int] = {}
    if isinstance(result, ImportFailure):
        return totals
    for row in result.rows:
        totals[date.fromisoformat(row.day)] = (
            totals.get(date.fromisoformat(row.day), 0) + row.total_tokens
        )
    return totals


def _cost_totals(result: CostImportResult) -> dict[date, Decimal]:
    totals: dict[date, Decimal] = {}
    if isinstance(result, ImportFailure):
        return totals
    for row in result.rows:
        try:
            amount = Decimal(row.amount)
        except InvalidOperation:
            amount = Decimal(0)
        totals[date.fromisoformat(row.day)] = (
            totals.get(date.fromisoformat(row.day), Decimal(0)) + amount
        )
    return totals


def build_reconciliation_report(
    *,
    since: date,
    until: date,
    codex: UsageImportResult,
    cc_switch: CostImportResult,
    omniroute: CostImportResult,
) -> ReconciliationReport:
    """Build the day-by-day three-source comparison for the given range."""
    if since > until:
        raise ValueError("reconciliation range must use ordered dates")
    codex_tokens = _token_totals(codex)
    cc_switch_costs = _cost_totals(cc_switch)
    omniroute_costs = _cost_totals(omniroute)

    rows: list[ReconciliationDayRow] = []
    current = since
    while current <= until:
        tokens = codex_tokens.get(current)
        cc_cost = cc_switch_costs.get(current)
        om_cost = omniroute_costs.get(current)
        cc_covered = not isinstance(cc_switch, ImportFailure)
        om_covered = not isinstance(omniroute, ImportFailure)
        notes: list[str] = []
        if tokens and cc_cost is not None and cc_cost == 0:
            notes.append("cc_switch_zero_cost_with_codex_tokens")
        if not cc_covered:
            notes.append("cc_switch_unavailable")
        if not om_covered:
            notes.append("omniroute_unavailable")
        rows.append(
            ReconciliationDayRow(
                day=current.isoformat(),
                codex_token_total=tokens,
                cc_switch_cost_usd=None if cc_cost is None else format(cc_cost, "f"),
                omniroute_cost_usd=None if om_cost is None else format(om_cost, "f"),
                cc_switch_covered=cc_covered,
                omniroute_covered=om_covered,
                notes=tuple(notes),
            )
        )
        current += timedelta(days=1)

    statuses = (
        ("codex", CODEX_SESSION_SOURCE_ID, _result_state(codex)),
        ("cc_switch", CC_SWITCH_COST_SOURCE_ID, _result_state(cc_switch)),
        ("omniroute", OMNIROUTE_COST_SOURCE_ID, _result_state(omniroute)),
    )
    return ReconciliationReport(since, until, tuple(rows), statuses)


def _result_state(result: UsageImportResult | CostImportResult) -> str:
    return "error" if isinstance(result, ImportFailure) else "ok"


def reconciliation_from_local_sources(
    *,
    since: date,
    until: date,
    session_roots: tuple | None = None,
    cc_switch_db=None,
    omniroute_usage=None,
) -> ReconciliationReport:
    """Build the report from the real local sources on this machine."""
    codex = CodexLocalDailyImporter(session_roots=session_roots).fetch_usage(
        since, until
    )
    cc_switch = CcSwitchCostImporter(db_path=cc_switch_db).fetch_costs(since, until)
    omniroute = OmniRouteCostImporter(usage_path=omniroute_usage).fetch_costs(
        since, until
    )
    return build_reconciliation_report(
        since=since,
        until=until,
        codex=codex,
        cc_switch=cc_switch,
        omniroute=omniroute,
    )
