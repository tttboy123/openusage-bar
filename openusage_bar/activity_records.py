"""Validated value objects shared by activity-ledger readers and writers."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from .config import ID_PATTERN
from .provider_catalog import PROVIDER_CATEGORIES, SOURCE_KINDS


_PROVIDER_INSTANCE_SOURCE_KINDS = SOURCE_KINDS | frozenset({"generic_https"})
_PRIVATE_LABEL_EMAIL = re.compile(
    r"[A-Z0-9._%+-]+@[A-Z0-9](?:[A-Z0-9.-]*[A-Z0-9])?", re.IGNORECASE
)
_PRIVATE_LABEL_PATH = re.compile(
    r"(?<![A-Z0-9._-])(?:"
    r"/(?!\s)[^\s()]+|"
    r"~[/\\](?!\s)[^\s()]+|"
    r"[A-Z]:[/\\](?!\s)[^\s()]+)",
    re.IGNORECASE,
)
_PRIVATE_LABEL_ASSIGNMENT = re.compile(
    r"(?<![A-Z0-9_])(?:authorization|credential|api[_ -]?key|password|secret|cookie|"
    r"access[_ -]?token|refresh[_ -]?token|token|username|user|"
    r"account(?:_email)?|email|attributes?|raw_attrs?|response_body|body)"
    r"\s*[:=]",
    re.IGNORECASE,
)
_PRIVATE_LABEL_CREDENTIAL = re.compile(
    r"(?:bearer\s+)[A-Z0-9._~+/=-]+|"
    r"eyJ[A-Z0-9_-]{8,}\.[A-Z0-9_-]{8,}\.[A-Z0-9_-]{8,}|"
    r"(?:sk-|ghp_|xox[baprs]-|AIza)[A-Z0-9_-]{16,}",
    re.IGNORECASE,
)
_PRIVATE_LABEL_CREDENTIAL_VALUE = re.compile(
    r"(?<![A-Z0-9_])(?:authorization|credential|api[_ -]?key|password|secret|"
    r"cookie|access[_ -]?token|refresh[_ -]?token|token)\s+"
    r"[A-Z0-9._~+/=-]{20,}(?=\s|$)",
    re.IGNORECASE,
)
_PRIVATE_LABEL_OPAQUE_KEY = re.compile(r"[A-Z0-9_~+/=-]{28,}", re.IGNORECASE)
_PRIVATE_LABEL_STRUCTURED = re.compile(r"(?:^|\s)[\[{]\s*[\"']")


def validate_id(name: str, value: str) -> None:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must use the stable identifier grammar")


def validate_day(value: str) -> None:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError("day must be an ISO local day") from error
    if parsed.isoformat() != value:
        raise ValueError("day must be an ISO local day")


def canonical_timestamp(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def validate_nonnegative_integer(
    name: str, value: int | None, *, nullable: bool = False
) -> None:
    if value is None and nullable:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def canonical_decimal(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{field} must be a finite decimal value")
    try:
        decimal = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{field} must be a finite decimal value") from error
    if not decimal.is_finite():
        raise ValueError(f"{field} must be a finite decimal value")
    text = format(decimal, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def validate_safe_display_name(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 128
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("display_name must be a safe provider label")
    opaque_key = any(
        any(character.isalpha() for character in match.group())
        and any(character.isdigit() for character in match.group())
        for match in _PRIVATE_LABEL_OPAQUE_KEY.finditer(value)
    )
    if (
        _PRIVATE_LABEL_EMAIL.search(value)
        or _PRIVATE_LABEL_PATH.search(value)
        or _PRIVATE_LABEL_ASSIGNMENT.search(value)
        or _PRIVATE_LABEL_CREDENTIAL.search(value)
        or _PRIVATE_LABEL_CREDENTIAL_VALUE.search(value)
        or _PRIVATE_LABEL_STRUCTURED.search(value)
        or opaque_key
    ):
        raise ValueError("display_name must be a safe provider label")


@dataclass(frozen=True)
class DailyUsageRow:
    day: str
    provider_id: str
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
    imported_at: str | None = None
    account_ref: str = ""

    def __post_init__(self) -> None:
        validate_day(self.day)
        validate_id("provider_id", self.provider_id)
        if self.account_ref:
            validate_id("account_ref", self.account_ref)
        validate_id("model_id", self.model_id)
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "total_tokens",
        ):
            validate_nonnegative_integer(field, getattr(self, field))
        validate_nonnegative_integer("reasoning_tokens", self.reasoning_tokens, nullable=True)
        object.__setattr__(self, "cost_amount", canonical_decimal(self.cost_amount, "cost_amount"))
        if self.cost_currency is not None:
            validate_id("cost_currency", self.cost_currency)
        if self.cost_basis is not None:
            validate_id("cost_basis", self.cost_basis)
        validate_id("quality", self.quality)
        imported_at = self.imported_at or datetime.now(timezone.utc).isoformat()
        object.__setattr__(self, "imported_at", canonical_timestamp(imported_at, "imported_at"))


@dataclass(frozen=True)
class DailyCostRow:
    day: str
    provider_id: str
    cost_kind: str
    currency: str
    amount: str
    basis: str
    quality: str
    imported_at: str | None = None
    account_ref: str = ""

    def __post_init__(self) -> None:
        validate_day(self.day)
        validate_id("provider_id", self.provider_id)
        if self.account_ref:
            validate_id("account_ref", self.account_ref)
        validate_id("cost_kind", self.cost_kind)
        if self.cost_kind != "actual":
            raise ValueError("cost_kind must be actual")
        validate_id("currency", self.currency)
        if self.currency != self.currency.upper() or not 3 <= len(self.currency) <= 8:
            raise ValueError("currency must be an uppercase currency identifier")
        validate_id("basis", self.basis)
        validate_id("quality", self.quality)
        amount = canonical_decimal(self.amount, "amount")
        if amount is None or Decimal(amount) < 0:
            raise ValueError("amount must be a nonnegative finite decimal value")
        object.__setattr__(self, "amount", amount)
        imported_at = self.imported_at or datetime.now(timezone.utc).isoformat()
        object.__setattr__(self, "imported_at", canonical_timestamp(imported_at, "imported_at"))


@dataclass(frozen=True)
class ProviderInstance:
    provider_id: str
    family_id: str
    display_name: str
    category: str
    credential_source: str
    source_kind: str
    observed_at: str
    revision: int = 0

    def __post_init__(self) -> None:
        validate_id("provider_id", self.provider_id)
        validate_id("family_id", self.family_id)
        validate_id("credential_source", self.credential_source)
        if self.category not in PROVIDER_CATEGORIES:
            raise ValueError("category must be a canonical provider category")
        if self.source_kind not in _PROVIDER_INSTANCE_SOURCE_KINDS:
            raise ValueError("source_kind must be a canonical source kind")
        validate_safe_display_name(self.display_name)
        object.__setattr__(self, "observed_at", canonical_timestamp(self.observed_at, "observed_at"))
        validate_nonnegative_integer("revision", self.revision)


@dataclass(frozen=True)
class DailyUsageRecord(DailyUsageRow):
    revision: int = 1
    payload_hash: str = ""
    record_id: str = ""
    source_id: str = "legacy"

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_id("source_id", self.source_id)


@dataclass(frozen=True)
class DailyCostRecord(DailyCostRow):
    revision: int = 1
    payload_hash: str = ""
    record_id: str = ""


@dataclass(frozen=True)
class QuotaObservation:
    record_id: str
    observed_at: str
    provider_id: str
    quota_name: str
    unit: str
    used: str | None
    quota_limit: str | None
    remaining: str | None
    remaining_ratio: float | None
    resets_at: str | None
    period_start: str | None
    period_end: str | None
    state: str
    quality: str
    stale: bool
    account_ref: str = ""

    def __post_init__(self) -> None:
        validate_id("record_id", self.record_id)
        validate_id("provider_id", self.provider_id)
        if self.account_ref:
            validate_id("account_ref", self.account_ref)
        if not isinstance(self.quota_name, str) or not self.quota_name.strip():
            raise ValueError("quota_name must not be empty")
        validate_id("unit", self.unit)
        validate_id("state", self.state)
        validate_id("quality", self.quality)
        object.__setattr__(self, "observed_at", canonical_timestamp(self.observed_at, "observed_at"))
        for field in ("resets_at", "period_start", "period_end"):
            object.__setattr__(self, field, canonical_timestamp(getattr(self, field), field))
        for field in ("used", "quota_limit", "remaining"):
            object.__setattr__(self, field, canonical_decimal(getattr(self, field), field))
        ratio = self.remaining_ratio
        if ratio is not None and (
            isinstance(ratio, bool)
            or not isinstance(ratio, (int, float))
            or not math.isfinite(ratio)
            or not 0 <= ratio <= 1
        ):
            raise ValueError("remaining_ratio must be between zero and one")
        if ratio is not None:
            canonical_ratio = float(ratio)
            object.__setattr__(self, "remaining_ratio", 0.0 if canonical_ratio == 0 else canonical_ratio)
        if not isinstance(self.stale, bool):
            raise ValueError("stale must be boolean")


@dataclass(frozen=True)
class QuotaState(QuotaObservation):
    revision: int = 1
    payload_hash: str = ""


@dataclass(frozen=True)
class QuotaSnapshot:
    snapshot_id: int
    record_id: str
    observed_at: str
    provider_id: str
    account_ref: str
    quota_name: str
    payload_json: str
    payload_hash: str


@dataclass(frozen=True)
class ChangeRecord:
    change_seq: int
    record_type: str
    record_id: str
    revision: int
    operation: str
    changed_at: str
    payload_json: str | None
    payload_hash: str


@dataclass(frozen=True)
class DailyUsageSnapshot:
    rows: tuple[DailyUsageRecord, ...]
    covered: frozenset[tuple[str, str, str]]
    coverage_sources: frozenset[tuple[str, str, str, str]]
    known_scopes: frozenset[tuple[str, str]]
    cursor: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))
        object.__setattr__(self, "covered", frozenset(self.covered))
        object.__setattr__(self, "coverage_sources", frozenset(self.coverage_sources))
        object.__setattr__(self, "known_scopes", frozenset(self.known_scopes))


@dataclass(frozen=True)
class DailyCostSnapshot:
    rows: tuple[DailyCostRecord, ...]
    covered: frozenset[tuple[str, str, str]]
    known_scopes: frozenset[tuple[str, str]]
    cursor: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))
        object.__setattr__(self, "covered", frozenset(self.covered))
        object.__setattr__(self, "known_scopes", frozenset(self.known_scopes))


@dataclass(frozen=True)
class QuotaStateSnapshot:
    rows: tuple[QuotaState, ...]
    cursor: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))


@dataclass(frozen=True)
class ProviderInstanceSnapshot:
    rows: tuple[ProviderInstance, ...]
    cursor: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))


@dataclass(frozen=True)
class QuotaHistorySnapshot:
    rows: tuple[QuotaSnapshot, ...]
    cursor: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))


@dataclass(frozen=True)
class SourceStatus:
    provider_id: str
    source_id: str
    state: str
    last_attempt_at: str
    last_success_at: str | None
    stale_at: str | None
    error_code: str | None


@dataclass(frozen=True)
class SourceStatusSnapshot:
    rows: tuple[SourceStatus, ...]
    cursor: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))


@dataclass(frozen=True)
class ChangeSnapshot:
    rows: tuple[ChangeRecord, ...]
    cursor: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))


@dataclass(frozen=True)
class PurgeResult:
    daily_rows: int
    coverage_rows: int
    quota_snapshots: int
    cost_rows: int = 0
    cost_coverage_rows: int = 0


@dataclass(frozen=True)
class UsageSummary:
    total_tokens: int
    model_count: int
    covered_day_count: int
    cursor: int
