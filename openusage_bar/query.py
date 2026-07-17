from __future__ import annotations

import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from .activity_store import ActivityStore
from .config import ID_PATTERN
from .provider_catalog import catalog


SCHEMA_VERSION = "1.0"
MAX_RANGE_DAYS = 731
MAX_LIMIT = 1000


def _utc_z(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def to_wire(value: Any) -> Any:
    """The single, version-stable conversion boundary for public payloads."""
    if is_dataclass(value) and not isinstance(value, type):
        return {_camel(field.name): to_wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (tuple, list)):
        return [to_wire(item) for item in value]
    if isinstance(value, (frozenset, set)):
        return [to_wire(item) for item in sorted(value, key=repr)]
    if isinstance(value, dict):
        return {str(key): to_wire(nested) for key, nested in value.items()}
    return value


@dataclass(frozen=True)
class ResultEnvelope:
    schema_version: str
    data_revision: int
    generated_at: str


@dataclass(frozen=True)
class SummaryResult(ResultEnvelope):
    today_tokens: int
    model_count: int
    covered_day_count: int


@dataclass(frozen=True)
class CapacityProvider:
    record_id: str
    provider_id: str
    account_ref: str | None
    quota_name: str
    unit: str
    used: str | None
    quota_limit: str | None
    remaining: str | None
    remaining_ratio: float | None
    resets_at: str | None
    period_start: str | None
    period_end: str | None
    observed_at: str
    freshness_seconds: int
    state: str
    quality: str
    stale: bool
    revision: int
    estimated_cost_per_million_tokens: str | None = None
    constraints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "constraints", tuple(self.constraints))


@dataclass(frozen=True)
class CapacityResult(ResultEnvelope):
    providers: tuple[CapacityProvider, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "providers", tuple(self.providers))


@dataclass(frozen=True)
class ActivityRow:
    day: str
    provider_id: str
    account_ref: str | None
    model_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    reasoning_tokens: int | None
    total_tokens: int
    cost_amount: str | None
    cost_currency: str | None
    cost_basis: str | None
    quality: str
    imported_at: str
    revision: int
    record_id: str
    source_id: str


@dataclass(frozen=True)
class CoverageRow:
    day: str
    provider_id: str
    account_ref: str | None
    covered: bool
    source_id: str | None


@dataclass(frozen=True)
class ActivityResult(ResultEnvelope):
    rows: tuple[ActivityRow, ...]
    coverage: tuple[CoverageRow, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))
        object.__setattr__(self, "coverage", tuple(self.coverage))


@dataclass(frozen=True)
class CostRow:
    day: str
    provider_id: str
    account_ref: str | None
    cost_kind: str
    amount: str
    currency: str
    basis: str
    quality: str
    imported_at: str
    revision: int
    record_id: str


@dataclass(frozen=True)
class CostCoverageRow:
    day: str
    provider_id: str
    account_ref: str | None
    covered: bool


@dataclass(frozen=True)
class CostsResult(ResultEnvelope):
    rows: tuple[CostRow, ...]
    coverage: tuple[CostCoverageRow, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))
        object.__setattr__(self, "coverage", tuple(self.coverage))


@dataclass(frozen=True)
class QuotaHistoryItem:
    snapshot_id: int
    record_id: str
    observed_at: str
    provider_id: str
    account_ref: str | None
    quota_name: str
    remaining_ratio: float | None
    state: str
    stale: bool


@dataclass(frozen=True)
class QuotaHistoryResult(ResultEnvelope):
    snapshots: tuple[QuotaHistoryItem, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshots", tuple(self.snapshots))


@dataclass(frozen=True)
class SourceStatusItem:
    provider_id: str
    source_id: str
    state: str
    last_attempt_at: str
    last_success_at: str | None
    stale_at: str | None
    error_code: str | None


@dataclass(frozen=True)
class SourceStatusResult(ResultEnvelope):
    sources: tuple[SourceStatusItem, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(self.sources))


@dataclass(frozen=True)
class ProviderInstanceItem:
    provider_id: str
    family_id: str
    display_name: str
    category: str
    credential_source: str
    source_kind: str
    observed_at: str
    revision: int


@dataclass(frozen=True)
class ProviderInstancesResult(ResultEnvelope):
    providers: tuple[ProviderInstanceItem, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "providers", tuple(self.providers))


@dataclass(frozen=True)
class ChangeItem:
    change_seq: int
    record_type: str
    record_id: str
    revision: int
    operation: str
    changed_at: str
    payload_json: str | None
    payload_hash: str


@dataclass(frozen=True)
class ChangePage(ResultEnvelope):
    records: tuple[ChangeItem, ...]
    next_cursor: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))


def _valid_day(value: date, name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError(f"{name} must be a date")
    return value


def _valid_ids(values: Iterable[str], name: str) -> tuple[str, ...]:
    result = tuple(values)
    if any(not isinstance(item, str) or ID_PATTERN.fullmatch(item) is None for item in result):
        raise ValueError(f"{name} must contain stable identifiers")
    return result


def _valid_limit(value: int | None, *, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LIMIT:
        raise ValueError("limit must be between 1 and 1000")
    return value


class QueryService:
    def __init__(self, store: ActivityStore, *, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _generated(self) -> tuple[datetime, str]:
        current = self.clock()
        generated = _utc_z(current)
        return current.astimezone(timezone.utc), generated

    def summary(self, today: date) -> SummaryResult:
        current_day = _valid_day(today, "today").isoformat()
        summary = self.store.summary(current_day, current_day)
        _, generated = self._generated()
        return SummaryResult(
            SCHEMA_VERSION, summary.cursor, generated, summary.total_tokens,
            summary.model_count, summary.covered_day_count,
        )

    def capacity(self, limit: int | None = None) -> CapacityResult:
        selected_limit = _valid_limit(limit)
        snapshot = self.store.snapshot_quota_states()
        generated_dt, generated = self._generated()
        groups: dict[tuple[str, str], list[Any]] = {}
        for state in snapshot.rows:
            groups.setdefault((state.provider_id, state.account_ref), []).append(state)

        def window_key(state: Any) -> tuple[Any, ...]:
            return (
                state.remaining_ratio is None,
                state.remaining_ratio if state.remaining_ratio is not None else 0.0,
                state.resets_at is None,
                state.resets_at or "",
                state.quota_name,
                state.record_id,
            )

        selected = [min(states, key=window_key) for states in groups.values()]
        selected.sort(key=lambda state: window_key(state)[:2] + (state.provider_id, state.account_ref))
        if selected_limit is not None:
            selected = selected[:selected_limit]
        providers = []
        for state in selected:
            observed = datetime.fromisoformat(state.observed_at.replace("Z", "+00:00"))
            providers.append(CapacityProvider(
                record_id=state.record_id,
                provider_id=state.provider_id,
                account_ref=state.account_ref or None,
                quota_name=state.quota_name,
                unit=state.unit,
                used=state.used,
                quota_limit=state.quota_limit,
                remaining=state.remaining,
                remaining_ratio=state.remaining_ratio,
                resets_at=state.resets_at,
                period_start=state.period_start,
                period_end=state.period_end,
                observed_at=state.observed_at,
                freshness_seconds=max(0, int((generated_dt - observed).total_seconds())),
                state=state.state,
                quality=state.quality,
                stale=state.stale,
                revision=state.revision,
            ))
        return CapacityResult(SCHEMA_VERSION, snapshot.cursor, generated, tuple(providers))

    def activity(
        self,
        from_day: date,
        to_day: date,
        provider_ids: Iterable[str] = (),
        model_ids: Iterable[str] = (),
    ) -> ActivityResult:
        start = _valid_day(from_day, "from_day")
        end = _valid_day(to_day, "to_day")
        if start > end:
            raise ValueError("from_day must not be after to_day")
        if (end - start).days + 1 > MAX_RANGE_DAYS:
            raise ValueError("activity range exceeds maximum")
        providers = frozenset(_valid_ids(provider_ids, "provider_ids"))
        models = frozenset(_valid_ids(model_ids, "model_ids"))
        snapshot = self.store.snapshot_daily_usage(start.isoformat(), end.isoformat())
        rows = tuple(ActivityRow(
            day=row.day, provider_id=row.provider_id, account_ref=row.account_ref or None,
            model_id=row.model_id, input_tokens=row.input_tokens, output_tokens=row.output_tokens,
            cache_read_tokens=row.cache_read_tokens, cache_creation_tokens=row.cache_creation_tokens,
            reasoning_tokens=row.reasoning_tokens, total_tokens=row.total_tokens,
            cost_amount=row.cost_amount, cost_currency=row.cost_currency, cost_basis=row.cost_basis,
            quality=row.quality, imported_at=row.imported_at or "", revision=row.revision,
            record_id=row.record_id, source_id=row.source_id,
        ) for row in snapshot.rows if (not providers or row.provider_id in providers) and (not models or row.model_id in models))
        scopes = {
            (row.provider_id, row.account_ref) for row in snapshot.rows
            if not providers or row.provider_id in providers
        } | {
            (provider_id, account_ref) for _, provider_id, account_ref in snapshot.covered
            if not providers or provider_id in providers
        } | {
            (provider_id, account_ref) for provider_id, account_ref in snapshot.known_scopes
            if not providers or provider_id in providers
        }
        for provider_id in providers:
            if not any(scope[0] == provider_id for scope in scopes):
                scopes.add((provider_id, ""))
        coverage_rows: list[CoverageRow] = []
        coverage_sources = {
            (day, provider_id, account_ref): source_id
            for day, provider_id, account_ref, source_id in snapshot.coverage_sources
        }
        current = start
        while current <= end:
            day = current.isoformat()
            for provider_id, account_ref in sorted(scopes):
                key = (day, provider_id, account_ref)
                coverage_rows.append(CoverageRow(
                    day, provider_id, account_ref or None,
                    key in snapshot.covered, coverage_sources.get(key),
                ))
            current += timedelta(days=1)
        coverage = tuple(coverage_rows)
        _, generated = self._generated()
        return ActivityResult(SCHEMA_VERSION, snapshot.cursor, generated, rows, coverage)

    def costs(
        self,
        from_day: date,
        to_day: date,
        provider_ids: Iterable[str] = (),
        currencies: Iterable[str] = (),
    ) -> CostsResult:
        start = _valid_day(from_day, "from_day")
        end = _valid_day(to_day, "to_day")
        if start > end:
            raise ValueError("from_day must not be after to_day")
        if (end - start).days + 1 > MAX_RANGE_DAYS:
            raise ValueError("cost range exceeds maximum")
        providers = frozenset(_valid_ids(provider_ids, "provider_ids"))
        selected_currencies = frozenset(_valid_ids(currencies, "currencies"))
        if any(currency != currency.upper() for currency in selected_currencies):
            raise ValueError("currencies must use uppercase identifiers")
        snapshot = self.store.snapshot_daily_costs(start.isoformat(), end.isoformat())
        rows = tuple(
            CostRow(
                day=row.day,
                provider_id=row.provider_id,
                account_ref=row.account_ref or None,
                cost_kind=row.cost_kind,
                amount=row.amount,
                currency=row.currency,
                basis=row.basis,
                quality=row.quality,
                imported_at=row.imported_at or "",
                revision=row.revision,
                record_id=row.record_id,
            )
            for row in snapshot.rows
            if (not providers or row.provider_id in providers)
            and (not selected_currencies or row.currency in selected_currencies)
        )
        scopes = {
            (row.provider_id, row.account_ref)
            for row in snapshot.rows
            if not providers or row.provider_id in providers
        } | {
            (provider_id, account_ref)
            for _, provider_id, account_ref in snapshot.covered
            if not providers or provider_id in providers
        } | {
            (provider_id, account_ref)
            for provider_id, account_ref in snapshot.known_scopes
            if not providers or provider_id in providers
        }
        for provider_id in providers:
            if not any(scope[0] == provider_id for scope in scopes):
                scopes.add((provider_id, ""))
        coverage_rows: list[CostCoverageRow] = []
        current = start
        while current <= end:
            day = current.isoformat()
            for provider_id, account_ref in sorted(scopes):
                coverage_rows.append(CostCoverageRow(
                    day=day,
                    provider_id=provider_id,
                    account_ref=account_ref or None,
                    covered=(day, provider_id, account_ref) in snapshot.covered,
                ))
            current += timedelta(days=1)
        _, generated = self._generated()
        return CostsResult(
            SCHEMA_VERSION, snapshot.cursor, generated, rows, tuple(coverage_rows)
        )

    def quota_history(
        self,
        *,
        provider_id: str | None = None,
        account_ref: str | None = None,
        from_time: str | None = None,
        to_time: str | None = None,
        limit: int = 1000,
    ) -> QuotaHistoryResult:
        selected_limit = _valid_limit(limit)
        if from_time is not None and to_time is not None:
            try:
                start = datetime.fromisoformat(from_time.replace("Z", "+00:00"))
                end = datetime.fromisoformat(to_time.replace("Z", "+00:00"))
            except (AttributeError, ValueError) as error:
                raise ValueError("quota history timestamps must be ISO timestamps") from error
            if start.tzinfo is None or end.tzinfo is None:
                raise ValueError("quota history timestamps must include a timezone")
            if end < start or (end - start).days > MAX_RANGE_DAYS:
                raise ValueError("quota history range is invalid or exceeds maximum")
        snapshot = self.store.snapshot_quota_history(
            provider_id=provider_id, account_ref=account_ref,
            from_time=from_time, to_time=to_time, limit=selected_limit or MAX_LIMIT,
        )
        items = []
        for row in snapshot.rows:
            payload = json.loads(row.payload_json)
            items.append(QuotaHistoryItem(
                row.snapshot_id, row.record_id, row.observed_at, row.provider_id,
                row.account_ref or None, row.quota_name, payload.get("remaining_ratio"),
                str(payload.get("state", "temporarily_unavailable")), bool(payload.get("stale", False)),
            ))
        _, generated = self._generated()
        return QuotaHistoryResult(SCHEMA_VERSION, snapshot.cursor, generated, tuple(items))

    def source_status(self) -> SourceStatusResult:
        snapshot = self.store.snapshot_source_statuses()
        sources = tuple(SourceStatusItem(
            row.provider_id, row.source_id, row.state, row.last_attempt_at,
            row.last_success_at, row.stale_at, row.error_code,
        ) for row in snapshot.rows)
        _, generated = self._generated()
        return SourceStatusResult(SCHEMA_VERSION, snapshot.cursor, generated, sources)

    def provider_instances(
        self, provider_ids: Iterable[str] = ()
    ) -> ProviderInstancesResult:
        selected = frozenset(_valid_ids(provider_ids, "provider_ids"))
        snapshot = self.store.snapshot_provider_instances()
        providers = tuple(
            ProviderInstanceItem(
                provider_id=row.provider_id,
                family_id=row.family_id,
                display_name=catalog.instance_display_name(
                    row.family_id, row.display_name
                ),
                category=row.category,
                credential_source=row.credential_source,
                source_kind=row.source_kind,
                observed_at=row.observed_at,
                revision=row.revision,
            )
            for row in snapshot.rows
            if not selected or row.provider_id in selected
        )
        _, generated = self._generated()
        return ProviderInstancesResult(
            SCHEMA_VERSION, snapshot.cursor, generated, providers
        )

    def changes(self, after: int, limit: int = 100) -> ChangePage:
        selected_limit = _valid_limit(limit)
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after must be a nonnegative integer")
        snapshot = self.store.snapshot_changes(after, selected_limit or 100)
        changes = tuple(ChangeItem(
            row.change_seq, row.record_type, row.record_id, row.revision, row.operation,
            row.changed_at, row.payload_json, row.payload_hash,
        ) for row in snapshot.rows)
        _, generated = self._generated()
        return ChangePage(
            SCHEMA_VERSION, snapshot.cursor, generated, changes,
            changes[-1].change_seq if changes else after,
        )
