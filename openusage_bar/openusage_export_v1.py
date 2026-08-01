from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any


CONTRACT = "openusage-export/v1"
SCHEMA_VERSION = "1"
MAX_RANGE_DAYS = 366
MAX_PAGE_SIZE = 1000
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_CURSOR_LENGTH = 2048
MAX_VERSION_LENGTH = 128
MAX_MODEL_LENGTH = 256

KINDS = ("capabilities", "daily_usage")
COVERAGE_STATES = ("complete", "partial", "none")
COUNTING_CONVENTIONS = (
    "components_disjoint",
    "input_includes_cache",
    "provider_reported",
)
QUALITIES = ("direct", "derived", "estimated")

_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_CURSOR = re.compile(r"^[^\x00-\x20\x7f=]{1,2048}$")
_FORBIDDEN_EXACT_KEYS = {
    "account",
    "account_id",
    "account_ref",
    "api_key",
    "authorization",
    "auth_token",
    "coding_plan_key",
    "cookie",
    "credentials",
    "email",
    "endpoint",
    "headers",
    "password",
    "project",
    "project_id",
    "prompt",
    "raw",
    "raw_payload",
    "request_body",
    "response",
    "response_body",
    "secret",
    "session",
    "session_id",
    "user_id",
}


class ExportDecodeError(ValueError):
    """A payload-safe contract error that never includes rejected values."""

    def __init__(self, code: str = "invalid_export_v1") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ExportCapabilities:
    contract: str
    schema_version: str
    generated_at: datetime
    openusage_version: str
    kinds: tuple[str, ...]
    provider_filter: str
    max_range_days: int
    max_page_size: int
    coverage_states: tuple[str, ...]
    token_counting_conventions: tuple[str, ...]
    qualities: tuple[str, ...]


@dataclass(frozen=True)
class ExportRequest:
    provider_id: str
    since: date
    until: date
    limit: int
    cursor: str | None


@dataclass(frozen=True)
class ExportCoverage:
    state: str
    since: date
    until: date


@dataclass(frozen=True)
class ExportDailyRow:
    day: date
    provider_id: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    reasoning_tokens: int | None
    total_tokens: int
    token_counting_convention: str
    quality: str


@dataclass(frozen=True)
class ExportPage:
    contract: str
    schema_version: str
    generated_at: datetime
    openusage_version: str
    request: ExportRequest
    coverage: ExportCoverage
    rows: tuple[ExportDailyRow, ...]
    next_cursor: str | None
    complete: bool

    @property
    def covered_zero(self) -> bool:
        return (
            self.coverage.state == "complete"
            and not self.rows
            and self.complete
            and self.request.cursor is None
        )


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ExportDecodeError()
    return value


def _required(mapping: dict[str, Any], key: str) -> Any:
    if key not in mapping:
        raise ExportDecodeError()
    return mapping[key]


def _safe_string(value: Any, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ExportDecodeError()
    return value


def _integer(value: Any, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ExportDecodeError()
    if maximum is not None and value > maximum:
        raise ExportDecodeError()
    return value


def _calendar_day(value: Any) -> date:
    text = _safe_string(value, 10)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as error:
        raise ExportDecodeError() from error
    if parsed.isoformat() != text:
        raise ExportDecodeError()
    return parsed


def _utc_datetime(value: Any) -> datetime:
    text = _safe_string(value, 64)
    if not text.endswith("Z"):
        raise ExportDecodeError()
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ExportDecodeError() from error
    if parsed.tzinfo != timezone.utc:
        raise ExportDecodeError()
    return parsed


def _forbid_private_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ExportDecodeError()
            normalized = key.lower().replace("-", "_")
            if normalized in _FORBIDDEN_EXACT_KEYS:
                raise ExportDecodeError("forbidden_export_field")
            _forbid_private_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _forbid_private_keys(nested)


def _document(payload: str | bytes | bytearray | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, dict):
        value: Any = payload
    else:
        if not isinstance(payload, (str, bytes, bytearray)):
            raise ExportDecodeError()
        size = len(payload.encode("utf-8")) if isinstance(payload, str) else len(payload)
        if size > MAX_DOCUMENT_BYTES:
            raise ExportDecodeError("export_too_large")
        try:
            value = json.loads(payload)
        except (json.JSONDecodeError, TypeError, UnicodeError) as error:
            raise ExportDecodeError("invalid_export_json") from error
    document = _mapping(value)
    _forbid_private_keys(document)
    return document


def _header(document: dict[str, Any], kind: str) -> tuple[datetime, str]:
    if (
        _required(document, "contract") != CONTRACT
        or _required(document, "schema_version") != SCHEMA_VERSION
        or _required(document, "kind") != kind
    ):
        raise ExportDecodeError()
    generated_at = _utc_datetime(_required(document, "generated_at"))
    version = _safe_string(_required(document, "openusage_version"), MAX_VERSION_LENGTH)
    return generated_at, version


def _exact_strings(value: Any, expected: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, list) or tuple(value) != expected:
        raise ExportDecodeError()
    return expected


def decode_capabilities(
    payload: str | bytes | bytearray | dict[str, Any],
) -> ExportCapabilities:
    document = _document(payload)
    generated_at, version = _header(document, "capabilities")
    raw = _mapping(_required(document, "capabilities"))
    kinds = _exact_strings(_required(raw, "kinds"), KINDS)
    coverage_states = _exact_strings(
        _required(raw, "coverage_states"), COVERAGE_STATES
    )
    conventions = _exact_strings(
        _required(raw, "token_counting_conventions"), COUNTING_CONVENTIONS
    )
    qualities = _exact_strings(_required(raw, "qualities"), QUALITIES)
    if _required(raw, "provider_filter") != "exact":
        raise ExportDecodeError()
    max_range = _integer(_required(raw, "max_range_days"), minimum=1)
    max_page = _integer(_required(raw, "max_page_size"), minimum=1)
    if max_range != MAX_RANGE_DAYS or max_page != MAX_PAGE_SIZE:
        raise ExportDecodeError()
    return ExportCapabilities(
        contract=CONTRACT,
        schema_version=SCHEMA_VERSION,
        generated_at=generated_at,
        openusage_version=version,
        kinds=kinds,
        provider_filter="exact",
        max_range_days=max_range,
        max_page_size=max_page,
        coverage_states=coverage_states,
        token_counting_conventions=conventions,
        qualities=qualities,
    )


def _decode_request(raw_value: Any) -> ExportRequest:
    raw = _mapping(raw_value)
    provider_id = _safe_string(_required(raw, "provider_id"), 128)
    if _PROVIDER_ID.fullmatch(provider_id) is None:
        raise ExportDecodeError()
    since = _calendar_day(_required(raw, "since"))
    until = _calendar_day(_required(raw, "until"))
    if until < since or (until - since).days >= MAX_RANGE_DAYS:
        raise ExportDecodeError()
    limit = _integer(_required(raw, "limit"), minimum=1, maximum=MAX_PAGE_SIZE)
    raw_cursor = raw.get("cursor")
    if raw_cursor is None:
        cursor = None
    else:
        cursor = _safe_string(raw_cursor, MAX_CURSOR_LENGTH)
        if _CURSOR.fullmatch(cursor) is None:
            raise ExportDecodeError()
    return ExportRequest(provider_id, since, until, limit, cursor)


def _decode_coverage(raw_value: Any, request: ExportRequest) -> ExportCoverage:
    raw = _mapping(raw_value)
    state = _required(raw, "state")
    if state not in COVERAGE_STATES:
        raise ExportDecodeError()
    since = _calendar_day(_required(raw, "since"))
    until = _calendar_day(_required(raw, "until"))
    if since != request.since or until != request.until:
        raise ExportDecodeError()
    return ExportCoverage(state, since, until)


def _decode_row(raw_value: Any, request: ExportRequest) -> ExportDailyRow:
    raw = _mapping(raw_value)
    day = _calendar_day(_required(raw, "day"))
    provider_id = _safe_string(_required(raw, "provider_id"), 128)
    model_id = _safe_string(_required(raw, "model_id"), MAX_MODEL_LENGTH)
    if (
        provider_id != request.provider_id
        or _MODEL_ID.fullmatch(model_id) is None
        or not request.since <= day <= request.until
    ):
        raise ExportDecodeError()
    input_tokens = _integer(_required(raw, "input_tokens"))
    output_tokens = _integer(_required(raw, "output_tokens"))
    cache_read_tokens = _integer(_required(raw, "cache_read_tokens"))
    cache_creation_tokens = _integer(_required(raw, "cache_creation_tokens"))
    reasoning_value = _required(raw, "reasoning_tokens")
    reasoning_tokens = (
        None if reasoning_value is None else _integer(reasoning_value)
    )
    total_tokens = _integer(_required(raw, "total_tokens"))
    convention = _required(raw, "token_counting_convention")
    quality = _required(raw, "quality")
    if convention not in COUNTING_CONVENTIONS or quality not in QUALITIES:
        raise ExportDecodeError()
    if convention == "components_disjoint":
        expected = (
            input_tokens
            + output_tokens
            + cache_read_tokens
            + cache_creation_tokens
            + (reasoning_tokens or 0)
        )
        if total_tokens != expected:
            raise ExportDecodeError()
    elif convention == "input_includes_cache":
        if total_tokens != input_tokens + output_tokens:
            raise ExportDecodeError()
    return ExportDailyRow(
        day,
        provider_id,
        model_id,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens,
        reasoning_tokens,
        total_tokens,
        convention,
        quality,
    )


def decode_daily(
    payload: str | bytes | bytearray | dict[str, Any],
) -> ExportPage:
    document = _document(payload)
    generated_at, version = _header(document, "daily_usage")
    request = _decode_request(_required(document, "request"))
    coverage = _decode_coverage(_required(document, "coverage"), request)
    raw_rows = _required(document, "rows")
    if not isinstance(raw_rows, list) or len(raw_rows) > request.limit:
        raise ExportDecodeError()
    rows = tuple(_decode_row(raw, request) for raw in raw_rows)
    identities = {(row.day, row.model_id) for row in rows}
    if len(identities) != len(rows):
        raise ExportDecodeError()
    page = _mapping(_required(document, "page"))
    complete = _required(page, "complete")
    if not isinstance(complete, bool):
        raise ExportDecodeError()
    raw_cursor = _required(page, "next_cursor")
    if complete:
        if raw_cursor is not None:
            raise ExportDecodeError()
        next_cursor = None
    else:
        next_cursor = _safe_string(raw_cursor, MAX_CURSOR_LENGTH)
        if _CURSOR.fullmatch(next_cursor) is None:
            raise ExportDecodeError()
    return ExportPage(
        CONTRACT,
        SCHEMA_VERSION,
        generated_at,
        version,
        request,
        coverage,
        rows,
        next_cursor,
        complete,
    )
