"""Bounded, content-free evidence storage for local route decisions."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .routing_contract import (
    MAX_ALTERNATIVES,
    MAX_COUNTER,
    MAX_REJECTED_TARGETS,
    RejectedTarget,
    RouteDecision,
    ScoreComponents,
    ScoredTarget,
)
from .routing_schema import (
    EXPECTED_INDEXES as _SCHEMA_INDEXES,
    EXPECTED_SCHEMA as _SCHEMA_COLUMNS,
    SCHEMA_VERSION,
)


MAX_DATABASE_BYTES = 16 * 1024 * 1024
MAX_DECISIONS = 10_000
MAX_ATTEMPTS = 30_000
MAX_CANDIDATES_JSON_BYTES = 256 * 1024
RETENTION = timedelta(days=7)
MAX_FUTURE_SKEW = timedelta(minutes=5)
MAX_PAGE_LIMIT = 100

_ROUTE_ID = re.compile(r"^route_[0-9a-f]{16,64}$")
_ATTEMPT_ID = re.compile(r"^attempt_[0-9a-f]{16,64}$")
_REQUEST_REF = re.compile(r"^req_[0-9a-f]{16,64}$")
_ANON_REF = re.compile(r"^anon_[0-9a-f]{16,64}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CURRENCY = re.compile(r"^[A-Z][A-Z0-9_]{2,7}$")

ATTEMPT_OUTCOMES = frozenset(
    {"succeeded", "transient_failure", "permanent_failure", "cancelled"}
)
ATTEMPT_STATUS_CLASSES = frozenset(
    {
        "success",
        "transport",
        "timeout",
        "http_408",
        "http_429",
        "http_5xx",
        "authentication",
        "invalid_request",
        "capability",
        "client_disconnect",
        "unknown",
    }
)
ATTEMPT_REASON_CODES = frozenset(
    {
        "provider_completed",
        "transport_error",
        "provider_timeout",
        "provider_rate_limited",
        "provider_unavailable",
        "authentication_failed",
        "invalid_request",
        "capability_error",
        "client_cancelled",
        "client_disconnected",
        "stream_started",
    }
)

_TABLES = frozenset({"routing_meta", "routing_decisions", "routing_attempts"})
_INDEXES = frozenset(_SCHEMA_INDEXES)
_EXPECTED_COLUMNS = {
    table: tuple(
        (name, column_type, not_null, primary_key)
        for name, column_type, not_null, _default, primary_key in columns
    )
    for table, columns in _SCHEMA_COLUMNS.items()
}


class RoutingStoreError(RuntimeError):
    """The routing evidence store could not safely apply an operation."""


def _match(name: str, value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _optional_match(
    name: str, value: object, pattern: re.Pattern[str]
) -> str | None:
    if value is None:
        return None
    return _match(name, value, pattern)


def _integer(name: str, value: object, *, minimum: int = 0, maximum: int = MAX_COUNTER) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{name} is invalid")
    return value


def _optional_integer(name: str, value: object) -> int | None:
    if value is None:
        return None
    return _integer(name, value)


def _utc_timestamp(name: str, value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{name} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{name} must be a UTC timestamp") from error
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a UTC timestamp")
    return parsed


def _canonical_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clock_value(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RoutingStoreError("routing evidence clock is unavailable")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class DecisionEvidence:
    decision_id: str
    client_request_ref: str | None
    session_ref: str | None
    decision: RouteDecision

    def __post_init__(self) -> None:
        _match("decision_id", self.decision_id, _ROUTE_ID)
        _optional_match("client_request_ref", self.client_request_ref, _REQUEST_REF)
        _optional_match("session_ref", self.session_ref, _ANON_REF)
        if not isinstance(self.decision, RouteDecision):
            raise ValueError("decision is invalid")


@dataclass(frozen=True)
class ExecutionAttemptEvidence:
    attempt_id: str
    decision_id: str
    target_id: str
    ordinal: int
    started_at: str
    completed_at: str
    outcome: str
    status_class: str | None
    reason_code: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_creation_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    cost_micros: int | None
    cost_currency: str | None

    def __post_init__(self) -> None:
        _match("attempt_id", self.attempt_id, _ATTEMPT_ID)
        _match("decision_id", self.decision_id, _ROUTE_ID)
        _match("target_id", self.target_id, _STABLE_ID)
        _integer("ordinal", self.ordinal, minimum=1, maximum=3)
        started = _utc_timestamp("started_at", self.started_at)
        completed = _utc_timestamp("completed_at", self.completed_at)
        if completed < started:
            raise ValueError("completed_at precedes started_at")
        if not isinstance(self.outcome, str) or self.outcome not in ATTEMPT_OUTCOMES:
            raise ValueError("outcome is invalid")
        _optional_match("status_class", self.status_class, _STABLE_ID)
        _optional_match("reason_code", self.reason_code, _STABLE_ID)
        if self.status_class is not None and self.status_class not in ATTEMPT_STATUS_CLASSES:
            raise ValueError("status_class is invalid")
        if self.reason_code is not None and self.reason_code not in ATTEMPT_REASON_CODES:
            raise ValueError("reason_code is invalid")
        token_values = (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_creation_tokens,
            self.total_tokens,
        )
        if any(value is None for value in token_values) != all(
            value is None for value in token_values
        ):
            raise ValueError("attempt token facts are incomplete")
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "reasoning_tokens",
            "total_tokens",
        ):
            _optional_integer(name, getattr(self, name))
        if (self.cost_micros is None) != (self.cost_currency is None):
            raise ValueError("attempt cost facts are incomplete")
        _optional_integer("cost_micros", self.cost_micros)
        if self.cost_currency is not None:
            _match("cost_currency", self.cost_currency, _CURRENCY)


@dataclass(frozen=True)
class StoredScoredTarget:
    target_id: str
    score: int
    components: ScoreComponents
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class StoredRejectedTarget:
    target_id: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class StoredDecision:
    decision_id: str
    created_at: str
    expires_at: str
    policy_id: str
    policy_revision: int
    data_revision: int
    runtime_revision: int | None
    client_request_ref: str | None
    session_ref: str | None
    selected_target_id: str | None
    selected_score: int | None
    selected: StoredScoredTarget | None
    alternatives: tuple[StoredScoredTarget, ...]
    rejected: tuple[StoredRejectedTarget, ...]


@dataclass(frozen=True)
class StoredAttempt:
    attempt_id: str
    decision_id: str
    target_id: str
    ordinal: int
    started_at: str
    completed_at: str
    outcome: str
    status_class: str | None
    reason_code: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_creation_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    cost_micros: int | None
    cost_currency: str | None


@dataclass(frozen=True)
class EvidenceWriteResult:
    stored: bool
    duplicate: bool
    pruned_decisions: int
    pruned_attempts: int
    revision: int


@dataclass(frozen=True)
class PruneResult:
    pruned_decisions: int
    pruned_attempts: int
    revision: int


def _scored_wire(value: ScoredTarget) -> dict[str, object]:
    return {
        "targetId": value.target_id,
        "score": value.score,
        "components": {
            "reliability": value.components.reliability,
            "headroom": value.components.headroom,
            "latency": value.components.latency,
            "cost": value.components.cost,
        },
        "reasons": list(value.reasons),
    }


def _candidates_json(decision: RouteDecision) -> str:
    payload = {
        "selected": None if decision.selected is None else _scored_wire(decision.selected),
        "alternatives": [_scored_wire(value) for value in decision.alternatives],
        "rejected": [
            {"targetId": value.target_id, "reasonCodes": list(value.reason_codes)}
            for value in decision.rejected
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_CANDIDATES_JSON_BYTES:
        raise RoutingStoreError("routing decision evidence is too large")
    return encoded


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decision_values(evidence: DecisionEvidence) -> tuple[object, ...]:
    value = evidence.decision
    return (
        evidence.decision_id,
        value.generated_at,
        value.expires_at,
        value.policy_id,
        value.policy_revision,
        value.data_revision,
        value.runtime_revision,
        evidence.client_request_ref,
        evidence.session_ref,
        None if value.selected is None else value.selected.target_id,
        None if value.selected is None else value.selected.score,
        _candidates_json(value),
    )


def _attempt_values(value: ExecutionAttemptEvidence) -> tuple[object, ...]:
    return (
        value.attempt_id,
        value.decision_id,
        value.target_id,
        value.ordinal,
        value.started_at,
        value.completed_at,
        value.outcome,
        value.status_class,
        value.reason_code,
        value.input_tokens,
        value.output_tokens,
        value.cache_read_tokens,
        value.cache_creation_tokens,
        value.reasoning_tokens,
        value.total_tokens,
        value.cost_micros,
        value.cost_currency,
    )


def _decode_scored(raw: object) -> StoredScoredTarget:
    if not isinstance(raw, dict) or set(raw) != {
        "targetId", "score", "components", "reasons"
    }:
        raise RoutingStoreError("routing decision evidence is incompatible")
    components = raw["components"]
    if not isinstance(components, dict) or set(components) != {
        "reliability", "headroom", "latency", "cost"
    }:
        raise RoutingStoreError("routing decision evidence is incompatible")
    try:
        score_components = ScoreComponents(
            reliability=components["reliability"],
            headroom=components["headroom"],
            latency=components["latency"],
            cost=components["cost"],
        )
        reasons_raw = raw["reasons"]
        if not isinstance(reasons_raw, list):
            raise ValueError("reasons are invalid")
        probe = ScoredTarget(
            target_id=raw["targetId"],
            provider_id="stored",
            account_ref="stored",
            model_id="stored",
            score=raw["score"],
            components=score_components,
            reasons=tuple(reasons_raw),
        )
    except (TypeError, ValueError) as error:
        raise RoutingStoreError("routing decision evidence is incompatible") from error
    return StoredScoredTarget(
        target_id=probe.target_id,
        score=probe.score,
        components=probe.components,
        reasons=probe.reasons,
    )


def _decode_candidates(
    encoded: object,
) -> tuple[
    StoredScoredTarget | None,
    tuple[StoredScoredTarget, ...],
    tuple[StoredRejectedTarget, ...],
]:
    if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > MAX_CANDIDATES_JSON_BYTES:
        raise RoutingStoreError("routing decision evidence is incompatible")
    try:
        raw = json.loads(encoded, object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError) as error:
        raise RoutingStoreError("routing decision evidence is incompatible") from error
    if not isinstance(raw, dict) or set(raw) != {"selected", "alternatives", "rejected"}:
        raise RoutingStoreError("routing decision evidence is incompatible")
    if json.dumps(raw, sort_keys=True, separators=(",", ":")) != encoded:
        raise RoutingStoreError("routing decision evidence is incompatible")
    selected = None if raw["selected"] is None else _decode_scored(raw["selected"])
    alternatives_raw = raw["alternatives"]
    rejected_raw = raw["rejected"]
    if (
        not isinstance(alternatives_raw, list)
        or len(alternatives_raw) > MAX_ALTERNATIVES
        or not isinstance(rejected_raw, list)
        or len(rejected_raw) > MAX_REJECTED_TARGETS
    ):
        raise RoutingStoreError("routing decision evidence is incompatible")
    alternatives = tuple(_decode_scored(value) for value in alternatives_raw)
    rejected: list[StoredRejectedTarget] = []
    try:
        for value in rejected_raw:
            if not isinstance(value, dict) or set(value) != {"targetId", "reasonCodes"}:
                raise ValueError("rejected target is invalid")
            reasons = value["reasonCodes"]
            if not isinstance(reasons, list):
                raise ValueError("rejected reasons are invalid")
            probe = RejectedTarget(value["targetId"], tuple(reasons))
            rejected.append(StoredRejectedTarget(probe.target_id, probe.reason_codes))
    except (TypeError, ValueError) as error:
        raise RoutingStoreError("routing decision evidence is incompatible") from error
    ids = (() if selected is None else (selected.target_id,)) + tuple(
        value.target_id for value in alternatives
    ) + tuple(value.target_id for value in rejected)
    if len(ids) != len(set(ids)):
        raise RoutingStoreError("routing decision evidence is incompatible")
    if selected is None and alternatives:
        raise RoutingStoreError("routing decision evidence is incompatible")
    if tuple(sorted(alternatives, key=lambda value: (-value.score, value.target_id))) != alternatives:
        raise RoutingStoreError("routing decision evidence is incompatible")
    if selected is not None and any(value.score > selected.score for value in alternatives):
        raise RoutingStoreError("routing decision evidence is incompatible")
    if tuple(sorted(rejected, key=lambda value: value.target_id)) != tuple(rejected):
        raise RoutingStoreError("routing decision evidence is incompatible")
    return selected, alternatives, tuple(rejected)


class RoutingStore:
    """Private SQLite store with strict schema, retention, row and byte caps."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        max_decisions: int = MAX_DECISIONS,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.max_decisions = _integer(
            "max_decisions", max_decisions, minimum=1, maximum=MAX_DECISIONS
        )
        self.max_attempts = _integer(
            "max_attempts", max_attempts, minimum=1, maximum=MAX_ATTEMPTS
        )
        self.path = str(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.page_limit_bytes = 0
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        identity: tuple[int, int] | None = None
        if self.path != ":memory:":
            identity = self._prepare_path(Path(self.path))
        try:
            self._connection = sqlite3.connect(
                self.path, timeout=5.0, check_same_thread=False
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA secure_delete=ON")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA trusted_schema=OFF")
            self._connection.execute("PRAGMA journal_mode=DELETE")
            if identity is not None:
                current = Path(self.path).lstat()
                if (current.st_dev, current.st_ino) != identity:
                    raise RuntimeError("routing database path is unavailable")
            version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError("routing database schema is newer than supported")
            quick_check = self._connection.execute("PRAGMA quick_check").fetchone()
            if quick_check is None or tuple(quick_check) != ("ok",):
                raise RuntimeError("routing database integrity is unavailable")
            object_count = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%'"
                ).fetchone()[0]
            )
            self._validate_existing_schema(
                require_all=version == SCHEMA_VERSION or object_count > 0
            )
            self._set_page_limit()
            self._initialize_schema()
            self._validate_existing_schema(require_all=True)
            with self._connection:
                self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except Exception:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            raise RuntimeError("routing database is unavailable") from None

    def __enter__(self) -> RoutingStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def revision(self) -> int:
        with self._lock:
            return self._revision_locked()

    def decision_count(self) -> int:
        with self._lock:
            connection = self._required_connection()
            return int(connection.execute("SELECT COUNT(*) FROM routing_decisions").fetchone()[0])

    def attempt_count(self) -> int:
        with self._lock:
            connection = self._required_connection()
            return int(connection.execute("SELECT COUNT(*) FROM routing_attempts").fetchone()[0])

    def record_decision(self, evidence: DecisionEvidence) -> EvidenceWriteResult:
        if not isinstance(evidence, DecisionEvidence):
            raise ValueError("decision evidence is invalid")
        values = _decision_values(evidence)
        now = _clock_value(self.clock)
        generated = _utc_timestamp("generated_at", evidence.decision.generated_at)
        if generated < now - RETENTION or generated > now + MAX_FUTURE_SKEW:
            raise RoutingStoreError("routing decision time is outside retention")
        with self._lock:
            connection = self._required_connection()
            existing = connection.execute(
                "SELECT decision_id,created_at,expires_at,policy_id,policy_revision,"
                "data_revision,runtime_revision,client_request_ref,session_ref,"
                "selected_target_id,selected_score,candidates_json "
                "FROM routing_decisions WHERE decision_id=?",
                (evidence.decision_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != values:
                    raise RoutingStoreError("routing decision identifier conflicts")
                return EvidenceWriteResult(True, True, 0, 0, self._revision_locked())
            try:
                with connection:
                    connection.execute(
                        "INSERT INTO routing_decisions("
                        "decision_id,created_at,expires_at,policy_id,policy_revision,"
                        "data_revision,runtime_revision,client_request_ref,session_ref,"
                        "selected_target_id,selected_score,candidates_json) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        values,
                    )
                    pruned_decisions, pruned_attempts = self._prune_locked(
                        _canonical_timestamp(now - RETENTION)
                    )
                    revision = self._advance_revision_locked()
            except sqlite3.Error as error:
                raise RoutingStoreError("routing decision evidence was not stored") from error
            stored = connection.execute(
                "SELECT 1 FROM routing_decisions WHERE decision_id=?",
                (evidence.decision_id,),
            ).fetchone() is not None
            return EvidenceWriteResult(stored, False, pruned_decisions, pruned_attempts, revision)

    def record_attempt(self, evidence: ExecutionAttemptEvidence) -> EvidenceWriteResult:
        if not isinstance(evidence, ExecutionAttemptEvidence):
            raise ValueError("attempt evidence is invalid")
        values = _attempt_values(evidence)
        now = _clock_value(self.clock)
        completed = _utc_timestamp("completed_at", evidence.completed_at)
        if completed < now - RETENTION or completed > now + MAX_FUTURE_SKEW:
            raise RoutingStoreError("routing attempt time is outside retention")
        with self._lock:
            connection = self._required_connection()
            existing = connection.execute(
                "SELECT attempt_id,decision_id,target_id,ordinal,started_at,completed_at,"
                "outcome,status_class,reason_code,input_tokens,output_tokens,"
                "cache_read_tokens,cache_creation_tokens,reasoning_tokens,total_tokens,"
                "cost_micros,cost_currency FROM routing_attempts WHERE attempt_id=?",
                (evidence.attempt_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != values:
                    raise RoutingStoreError("routing attempt identifier conflicts")
                return EvidenceWriteResult(True, True, 0, 0, self._revision_locked())
            decision = connection.execute(
                "SELECT candidates_json FROM routing_decisions WHERE decision_id=?",
                (evidence.decision_id,),
            ).fetchone()
            if decision is None:
                raise RoutingStoreError("routing attempt decision is unavailable")
            selected, alternatives, _ = _decode_candidates(decision[0])
            allowed = (() if selected is None else (selected.target_id,)) + tuple(
                value.target_id for value in alternatives
            )
            if evidence.target_id not in allowed:
                raise RoutingStoreError("routing attempt target is unavailable")
            try:
                with connection:
                    connection.execute(
                        "INSERT INTO routing_attempts("
                        "attempt_id,decision_id,target_id,ordinal,started_at,completed_at,"
                        "outcome,status_class,reason_code,input_tokens,output_tokens,"
                        "cache_read_tokens,cache_creation_tokens,reasoning_tokens,total_tokens,"
                        "cost_micros,cost_currency) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        values,
                    )
                    pruned_decisions, pruned_attempts = self._prune_locked(
                        _canonical_timestamp(now - RETENTION)
                    )
                    revision = self._advance_revision_locked()
            except sqlite3.Error as error:
                raise RoutingStoreError("routing attempt evidence was not stored") from error
            stored = connection.execute(
                "SELECT 1 FROM routing_attempts WHERE attempt_id=?",
                (evidence.attempt_id,),
            ).fetchone() is not None
            return EvidenceWriteResult(stored, False, pruned_decisions, pruned_attempts, revision)

    def get_decision(self, decision_id: str) -> StoredDecision | None:
        _match("decision_id", decision_id, _ROUTE_ID)
        with self._lock:
            row = self._required_connection().execute(
                "SELECT decision_id,created_at,expires_at,policy_id,policy_revision,"
                "data_revision,runtime_revision,client_request_ref,session_ref,"
                "selected_target_id,selected_score,candidates_json "
                "FROM routing_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            return None if row is None else self._stored_decision(row)

    def list_decisions(
        self, *, before: str | None = None, limit: int = 50
    ) -> tuple[StoredDecision, ...]:
        _integer("limit", limit, minimum=1, maximum=MAX_PAGE_LIMIT)
        with self._lock:
            connection = self._required_connection()
            parameters: tuple[object, ...]
            where = ""
            if before is None:
                parameters = (limit,)
            else:
                _match("before", before, _ROUTE_ID)
                cursor = connection.execute(
                    "SELECT created_at,decision_id FROM routing_decisions WHERE decision_id=?",
                    (before,),
                ).fetchone()
                if cursor is None:
                    raise RoutingStoreError("routing decision cursor is unavailable")
                where = "WHERE created_at < ? OR (created_at = ? AND decision_id < ?) "
                parameters = (cursor[0], cursor[0], cursor[1], limit)
            rows = connection.execute(
                "SELECT decision_id,created_at,expires_at,policy_id,policy_revision,"
                "data_revision,runtime_revision,client_request_ref,session_ref,"
                "selected_target_id,selected_score,candidates_json FROM routing_decisions "
                f"{where}ORDER BY created_at DESC,decision_id DESC LIMIT ?",
                parameters,
            ).fetchall()
            return tuple(self._stored_decision(row) for row in rows)

    def attempts_for_decision(self, decision_id: str) -> tuple[StoredAttempt, ...]:
        _match("decision_id", decision_id, _ROUTE_ID)
        with self._lock:
            rows = self._required_connection().execute(
                "SELECT attempt_id,decision_id,target_id,ordinal,started_at,completed_at,"
                "outcome,status_class,reason_code,input_tokens,output_tokens,"
                "cache_read_tokens,cache_creation_tokens,reasoning_tokens,total_tokens,"
                "cost_micros,cost_currency FROM routing_attempts WHERE decision_id=? "
                "ORDER BY ordinal,attempt_id",
                (decision_id,),
            ).fetchall()
            return tuple(self._stored_attempt(row) for row in rows)

    def prune(self) -> PruneResult:
        now = _clock_value(self.clock)
        with self._lock:
            connection = self._required_connection()
            try:
                with connection:
                    decisions, attempts = self._prune_locked(
                        _canonical_timestamp(now - RETENTION)
                    )
                    revision = (
                        self._advance_revision_locked()
                        if decisions or attempts
                        else self._revision_locked()
                    )
            except sqlite3.Error as error:
                raise RoutingStoreError("routing evidence pruning failed") from error
            return PruneResult(decisions, attempts, revision)

    def _stored_decision(self, row: sqlite3.Row) -> StoredDecision:
        selected, alternatives, rejected = _decode_candidates(row[11])
        if (None if selected is None else selected.target_id) != row[9] or (
            None if selected is None else selected.score
        ) != row[10]:
            raise RoutingStoreError("routing decision evidence is incompatible")
        try:
            _match("decision_id", row[0], _ROUTE_ID)
            _utc_timestamp("created_at", row[1])
            _utc_timestamp("expires_at", row[2])
            _match("policy_id", row[3], _STABLE_ID)
            _integer("policy_revision", row[4])
            _integer("data_revision", row[5])
            _optional_integer("runtime_revision", row[6])
            _optional_match("client_request_ref", row[7], _REQUEST_REF)
            _optional_match("session_ref", row[8], _ANON_REF)
        except (TypeError, ValueError) as error:
            raise RoutingStoreError("routing decision evidence is incompatible") from error
        return StoredDecision(
            decision_id=row[0],
            created_at=row[1],
            expires_at=row[2],
            policy_id=row[3],
            policy_revision=row[4],
            data_revision=row[5],
            runtime_revision=row[6],
            client_request_ref=row[7],
            session_ref=row[8],
            selected_target_id=row[9],
            selected_score=row[10],
            selected=selected,
            alternatives=alternatives,
            rejected=rejected,
        )

    @staticmethod
    def _stored_attempt(row: sqlite3.Row) -> StoredAttempt:
        try:
            evidence = ExecutionAttemptEvidence(*tuple(row))
        except (TypeError, ValueError) as error:
            raise RoutingStoreError("routing attempt evidence is incompatible") from error
        return StoredAttempt(*_attempt_values(evidence))

    def _required_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RoutingStoreError("routing evidence store is closed")
        return self._connection

    def _revision_locked(self) -> int:
        row = self._required_connection().execute(
            "SELECT value FROM routing_meta WHERE key='revision'"
        ).fetchone()
        if row is None or isinstance(row[0], bool) or not isinstance(row[0], int) or row[0] < 0:
            raise RoutingStoreError("routing evidence revision is invalid")
        return int(row[0])

    def _advance_revision_locked(self) -> int:
        current = self._revision_locked()
        if current >= MAX_COUNTER:
            raise RoutingStoreError("routing evidence revision is exhausted")
        revision = current + 1
        self._required_connection().execute(
            "UPDATE routing_meta SET value=? WHERE key='revision'", (revision,)
        )
        return revision

    def _prune_locked(self, cutoff: str) -> tuple[int, int]:
        connection = self._required_connection()
        decisions_before = int(
            connection.execute("SELECT COUNT(*) FROM routing_decisions").fetchone()[0]
        )
        attempts_before = int(
            connection.execute("SELECT COUNT(*) FROM routing_attempts").fetchone()[0]
        )
        connection.execute(
            "DELETE FROM routing_attempts WHERE completed_at < ?", (cutoff,)
        )
        connection.execute("DELETE FROM routing_decisions WHERE created_at < ?", (cutoff,))
        decision_overflow = max(
            0,
            int(connection.execute("SELECT COUNT(*) FROM routing_decisions").fetchone()[0])
            - self.max_decisions,
        )
        if decision_overflow:
            connection.execute(
                "DELETE FROM routing_decisions WHERE decision_id IN ("
                "SELECT decision_id FROM routing_decisions "
                "ORDER BY created_at,rowid LIMIT ?)",
                (decision_overflow,),
            )
        attempt_overflow = max(
            0,
            int(connection.execute("SELECT COUNT(*) FROM routing_attempts").fetchone()[0])
            - self.max_attempts,
        )
        if attempt_overflow:
            connection.execute(
                "DELETE FROM routing_attempts WHERE attempt_id IN ("
                "SELECT attempt_id FROM routing_attempts "
                "ORDER BY completed_at,rowid LIMIT ?)",
                (attempt_overflow,),
            )
        decisions_after = int(
            connection.execute("SELECT COUNT(*) FROM routing_decisions").fetchone()[0]
        )
        attempts_after = int(
            connection.execute("SELECT COUNT(*) FROM routing_attempts").fetchone()[0]
        )
        return decisions_before - decisions_after, attempts_before - attempts_after

    @staticmethod
    def _prepare_path(path: Path) -> tuple[int, int]:
        if not path.is_absolute():
            raise RuntimeError("routing database path is unavailable")
        try:
            parent = path.parent.lstat()
        except OSError:
            raise RuntimeError("routing database path is unavailable") from None
        if (
            stat.S_ISLNK(parent.st_mode)
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise RuntimeError("routing database path is unavailable")
        try:
            existing = path.lstat()
        except FileNotFoundError:
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(path, flags, 0o600)
                os.fchmod(descriptor, 0o600)
                created = os.fstat(descriptor)
            except OSError:
                raise RuntimeError("routing database path is unavailable") from None
            finally:
                if "descriptor" in locals():
                    os.close(descriptor)
            return created.st_dev, created.st_ino
        if (
            stat.S_ISLNK(existing.st_mode)
            or not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != os.getuid()
            or stat.S_IMODE(existing.st_mode) != 0o600
        ):
            raise RuntimeError("routing database path is unavailable")
        return existing.st_dev, existing.st_ino

    def _set_page_limit(self) -> None:
        connection = self._required_connection()
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        requested = MAX_DATABASE_BYTES // page_size
        actual = int(connection.execute(f"PRAGMA max_page_count={requested}").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        if actual * page_size > MAX_DATABASE_BYTES or page_count > actual:
            raise RuntimeError("routing database exceeds its size limit")
        self.page_limit_bytes = actual * page_size

    def _validate_existing_schema(self, *, require_all: bool = False) -> None:
        connection = self._required_connection()
        forbidden_objects = connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('view','trigger')"
        ).fetchall()
        if forbidden_objects:
            raise RuntimeError("routing database contains incompatible objects")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if tables - _TABLES or indexes - _INDEXES:
            raise RuntimeError("routing database schema is incompatible")
        for table in tables:
            actual = tuple(
                (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            )
            if actual != _EXPECTED_COLUMNS[table]:
                raise RuntimeError("routing database schema is incompatible")
        if require_all and (tables != _TABLES or indexes != _INDEXES):
            raise RuntimeError("routing database schema is incomplete")
        if require_all:
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                raise RuntimeError("routing database constraints are unavailable")
            foreign_keys = tuple(
                tuple(row)
                for row in connection.execute(
                    "PRAGMA foreign_key_list(routing_attempts)"
                ).fetchall()
            )
            if foreign_keys != (
                (
                    0,
                    0,
                    "routing_decisions",
                    "decision_id",
                    "decision_id",
                    "NO ACTION",
                    "CASCADE",
                    "NONE",
                ),
            ):
                raise RuntimeError("routing database constraints are incompatible")
            unique_attempt_ordinals = False
            for index in connection.execute("PRAGMA index_list(routing_attempts)"):
                columns = tuple(
                    row[2]
                    for row in connection.execute(
                        f"PRAGMA index_info({json.dumps(str(index[1]))})"
                    ).fetchall()
                )
                if int(index[2]) == 1 and columns == ("decision_id", "ordinal"):
                    unique_attempt_ordinals = True
            if not unique_attempt_ordinals:
                raise RuntimeError("routing database constraints are incompatible")
            for index_name, (_table, _unique, columns) in _SCHEMA_INDEXES.items():
                expected_columns = tuple(name for name, _descending in columns)
                actual_columns = tuple(
                    row[2]
                    for row in connection.execute(
                        f"PRAGMA index_info({json.dumps(index_name)})"
                    ).fetchall()
                )
                if actual_columns != expected_columns:
                    raise RuntimeError("routing database indexes are incompatible")
            meta_rows = tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT key,value FROM routing_meta ORDER BY key"
                ).fetchall()
            )
            if (
                len(meta_rows) != 1
                or meta_rows[0][0] != "revision"
                or isinstance(meta_rows[0][1], bool)
                or not isinstance(meta_rows[0][1], int)
                or not 0 <= meta_rows[0][1] <= MAX_COUNTER
            ):
                raise RuntimeError("routing database metadata is incompatible")

    def _initialize_schema(self) -> None:
        connection = self._required_connection()
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS routing_meta ("
                "key TEXT PRIMARY KEY,value INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS routing_decisions ("
                "decision_id TEXT PRIMARY KEY,created_at TEXT NOT NULL,"
                "expires_at TEXT NOT NULL,policy_id TEXT NOT NULL,"
                "policy_revision INTEGER NOT NULL,data_revision INTEGER NOT NULL,"
                "runtime_revision INTEGER,client_request_ref TEXT,session_ref TEXT,"
                "selected_target_id TEXT,selected_score INTEGER,"
                "candidates_json TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS routing_attempts ("
                "attempt_id TEXT PRIMARY KEY,decision_id TEXT NOT NULL,"
                "target_id TEXT NOT NULL,ordinal INTEGER NOT NULL,"
                "started_at TEXT NOT NULL,completed_at TEXT NOT NULL,"
                "outcome TEXT NOT NULL,status_class TEXT,reason_code TEXT,"
                "input_tokens INTEGER,output_tokens INTEGER,cache_read_tokens INTEGER,"
                "cache_creation_tokens INTEGER,reasoning_tokens INTEGER,total_tokens INTEGER,"
                "cost_micros INTEGER,cost_currency TEXT,"
                "FOREIGN KEY(decision_id) REFERENCES routing_decisions(decision_id) "
                "ON DELETE CASCADE,UNIQUE(decision_id,ordinal))"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS routing_decisions_created "
                "ON routing_decisions(created_at,decision_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS routing_attempts_completed "
                "ON routing_attempts(completed_at,attempt_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS routing_attempts_decision "
                "ON routing_attempts(decision_id,ordinal)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO routing_meta(key,value) VALUES('revision',0)"
            )


def try_record_decision(store: object, evidence: DecisionEvidence) -> bool:
    """Best-effort logging that can never replace or mutate a route selection."""
    try:
        result = store.record_decision(evidence)  # type: ignore[attr-defined]
    except Exception:
        return False
    return isinstance(result, EvidenceWriteResult) and result.stored


def try_record_attempt(store: object, evidence: ExecutionAttemptEvidence) -> bool:
    """Best-effort execution logging without surfacing Provider error material."""
    try:
        result = store.record_attempt(evidence)  # type: ignore[attr-defined]
    except Exception:
        return False
    return isinstance(result, EvidenceWriteResult) and result.stored
