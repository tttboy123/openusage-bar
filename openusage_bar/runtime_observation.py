"""Strict, privacy-bounded request observation values.

This module deliberately has no dependency on the durable activity ledger.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


SCHEMA_VERSION = 1
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_BATCH_SIZE = 256
MAX_COUNTER = 1_000_000_000_000
MAX_REQUEST_DURATION = timedelta(hours=24)

TERMINAL_STATUSES = frozenset({"completed", "error", "cancelled", "timeout"})
TOKEN_COUNTING_CONVENTIONS = frozenset({
    "components_disjoint",
    "input_includes_cache",
    "provider_reported",
})
QUALITIES = frozenset({"provider_reported", "derived", "estimated"})

_OBSERVATION_ID = re.compile(r"^obs_[0-9a-f]{32}$")
_SCOPE_REF = re.compile(r"^anon_[0-9a-f]{16,64}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CURRENCY = re.compile(r"^[a-z]{3}$")

_DOCUMENT_KEYS = frozenset({"schemaVersion", "observations"})
_OBSERVATION_KEYS = frozenset({
    "observationId",
    "providerId",
    "modelId",
    "scopeRef",
    "startedAt",
    "firstTokenAt",
    "completedAt",
    "inputTokens",
    "outputTokens",
    "cacheReadTokens",
    "cacheCreationTokens",
    "reasoningTokens",
    "totalTokens",
    "tokenCountingConvention",
    "status",
    "costMicros",
    "costCurrency",
    "sourceId",
    "quality",
})


class RuntimeObservationDecodeError(ValueError):
    """A runtime document failed strict, sanitized validation."""


@dataclass(frozen=True)
class RuntimeObservation:
    observation_id: str
    provider_id: str
    model_id: str
    scope_ref: str
    started_at: datetime
    first_token_at: datetime | None
    completed_at: datetime
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    reasoning_tokens: int | None
    total_tokens: int
    token_counting_convention: str
    status: str
    cost_micros: int | None
    cost_currency: str | None
    source_id: str
    quality: str

    @property
    def ttft_ms(self) -> int | None:
        if self.first_token_at is None:
            return None
        return _milliseconds(self.first_token_at - self.started_at)

    @property
    def duration_ms(self) -> int:
        return _milliseconds(self.completed_at - self.started_at)


@dataclass(frozen=True)
class RuntimeIngestDocument:
    schema_version: int
    observations: tuple[RuntimeObservation, ...]


def canonical_runtime_timestamp(value: datetime) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("runtime timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _milliseconds(value: timedelta) -> int:
    return value.days * 86_400_000 + value.seconds * 1000 + value.microseconds // 1000


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeObservationDecodeError("invalid runtime observation document")
        result[key] = value
    return result


def _mapping(value: Any, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise RuntimeObservationDecodeError("invalid runtime observation document")
    return value


def _string(value: Any, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise RuntimeObservationDecodeError("invalid runtime observation document")
    return value


def _integer(value: Any, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_COUNTER
    ):
        raise RuntimeObservationDecodeError("invalid runtime observation document")
    return value


def _timestamp(value: Any, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RuntimeObservationDecodeError("invalid runtime observation document")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise RuntimeObservationDecodeError(
            "invalid runtime observation document"
        ) from error
    if parsed.utcoffset() != timedelta(0):
        raise RuntimeObservationDecodeError("invalid runtime observation document")
    return parsed.astimezone(timezone.utc)


def _decode_observation(value: Any) -> RuntimeObservation:
    raw = _mapping(value, _OBSERVATION_KEYS)
    observation_id = _string(raw["observationId"], _OBSERVATION_ID)
    provider_id = _string(raw["providerId"], _STABLE_ID)
    model_id = _string(raw["modelId"], _STABLE_ID)
    scope_ref = _string(raw["scopeRef"], _SCOPE_REF)
    source_id = _string(raw["sourceId"], _STABLE_ID)
    started_at = _timestamp(raw["startedAt"])
    first_token_at = _timestamp(raw["firstTokenAt"], nullable=True)
    completed_at = _timestamp(raw["completedAt"])
    assert started_at is not None and completed_at is not None
    if (
        completed_at < started_at
        or completed_at - started_at > MAX_REQUEST_DURATION
        or first_token_at is not None
        and not started_at <= first_token_at <= completed_at
    ):
        raise RuntimeObservationDecodeError("invalid runtime observation document")

    input_tokens = _integer(raw["inputTokens"])
    output_tokens = _integer(raw["outputTokens"])
    cache_read_tokens = _integer(raw["cacheReadTokens"])
    cache_creation_tokens = _integer(raw["cacheCreationTokens"])
    reasoning_tokens = _integer(raw["reasoningTokens"], nullable=True)
    total_tokens = _integer(raw["totalTokens"])
    assert isinstance(input_tokens, int)
    assert isinstance(output_tokens, int)
    assert isinstance(cache_read_tokens, int)
    assert isinstance(cache_creation_tokens, int)
    assert isinstance(total_tokens, int)
    convention = raw["tokenCountingConvention"]
    if convention not in TOKEN_COUNTING_CONVENTIONS:
        raise RuntimeObservationDecodeError("invalid runtime observation document")
    if convention == "components_disjoint":
        known = (
            input_tokens
            + output_tokens
            + cache_read_tokens
            + cache_creation_tokens
        )
        if reasoning_tokens is None:
            if total_tokens < known:
                raise RuntimeObservationDecodeError(
                    "invalid runtime observation document"
                )
        elif total_tokens != known + reasoning_tokens:
            raise RuntimeObservationDecodeError("invalid runtime observation document")
    elif convention == "input_includes_cache":
        if (
            cache_read_tokens + cache_creation_tokens > input_tokens
            or total_tokens != input_tokens + output_tokens
            or reasoning_tokens is not None
            and reasoning_tokens > output_tokens
        ):
            raise RuntimeObservationDecodeError("invalid runtime observation document")

    status = raw["status"]
    quality = raw["quality"]
    if status not in TERMINAL_STATUSES or quality not in QUALITIES:
        raise RuntimeObservationDecodeError("invalid runtime observation document")

    cost_micros = _integer(raw["costMicros"], nullable=True)
    cost_currency = raw["costCurrency"]
    if (cost_micros is None) != (cost_currency is None):
        raise RuntimeObservationDecodeError("invalid runtime observation document")
    if cost_currency is not None:
        cost_currency = _string(cost_currency, _CURRENCY)

    return RuntimeObservation(
        observation_id=observation_id,
        provider_id=provider_id,
        model_id=model_id,
        scope_ref=scope_ref,
        started_at=started_at,
        first_token_at=first_token_at,
        completed_at=completed_at,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        token_counting_convention=convention,
        status=status,
        cost_micros=cost_micros,
        cost_currency=cost_currency,
        source_id=source_id,
        quality=quality,
    )


def decode_runtime_document(
    payload: str | bytes | bytearray,
) -> RuntimeIngestDocument:
    if isinstance(payload, str):
        encoded = payload.encode("utf-8")
    elif isinstance(payload, (bytes, bytearray)):
        encoded = bytes(payload)
    else:
        raise RuntimeObservationDecodeError("invalid runtime observation document")
    if not encoded or len(encoded) > MAX_DOCUMENT_BYTES:
        raise RuntimeObservationDecodeError("invalid runtime observation document")
    try:
        raw = json.loads(encoded.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RuntimeObservationDecodeError) as error:
        raise RuntimeObservationDecodeError(
            "invalid runtime observation document"
        ) from error
    document = _mapping(raw, _DOCUMENT_KEYS)
    if document["schemaVersion"] != SCHEMA_VERSION:
        raise RuntimeObservationDecodeError("invalid runtime observation document")
    rows = document["observations"]
    if not isinstance(rows, list) or len(rows) > MAX_BATCH_SIZE:
        raise RuntimeObservationDecodeError("invalid runtime observation document")
    observations = tuple(_decode_observation(row) for row in rows)
    identifiers = tuple(row.observation_id for row in observations)
    if len(identifiers) != len(set(identifiers)):
        raise RuntimeObservationDecodeError("invalid runtime observation document")
    return RuntimeIngestDocument(SCHEMA_VERSION, observations)
