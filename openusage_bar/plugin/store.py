"""Private persistent idempotency and decision storage for Plugin API v1."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from ..windows_file_security import native_windows_file_security

from .contracts import (
    DECISION_ID_PATTERN,
    IDEMPOTENCY_KEY_PATTERN,
    IDEMPOTENCY_RETENTION_SECONDS,
    MAX_IDEMPOTENCY_RECORDS_PER_PRINCIPAL,
    PRINCIPALS,
    canonical_json,
    problem,
    utc_fixed6,
)


class IdempotencyConflict(RuntimeError):
    pass


class IdempotencyInProgress(RuntimeError):
    pass


class IdempotencyCapacityExceeded(RuntimeError):
    pass


class DecisionNotFound(RuntimeError):
    pass


class DecisionOutcomeConflict(RuntimeError):
    pass


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Condition] = {}
_WINDOWS_FILE_SECURITY = native_windows_file_security()


@dataclass(frozen=True)
class _DatabaseIdentity:
    device: int
    inode: int


class _SQLMutation(Protocol):
    def __call__(
        self, connection: sqlite3.Connection
    ) -> tuple[int, dict[str, object]] | None: ...


_SCHEMA_COLUMNS = {
    "plugin_schema": ("version",),
    "idempotency_records": (
        "principal", "route", "key_hash", "request_digest", "state",
        "status_code", "response_json", "owner_pid", "created_at", "expires_at",
    ),
    "decisions": (
        "principal", "decision_id", "decision_json", "outcome_json",
        "created_at", "expires_at",
    ),
    "plugin_connections": (
        "principal", "last_seen_at", "capabilities_negotiated",
        "last_sync_outcome", "last_sync_at",
    ),
}


def _condition(path: Path) -> threading.Condition:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Condition())


def _process_is_alive(value: object) -> bool:
    if type(value) is not int or value <= 0:
        return False
    if value == os.getpid():
        return True
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _prepare_private_database(path: Path) -> _DatabaseIdentity:
    if not path.is_absolute() or ".." in path.parts or path.name != "plugin.sqlite3":
        raise ValueError("invalid Plugin database path")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise ValueError("unsafe Plugin database parent")
    if hasattr(os, "getuid") and parent.st_uid != os.getuid():
        raise ValueError("unsafe Plugin database parent")
    if os.name == "nt" and _WINDOWS_FILE_SECURITY is None:
        raise ValueError("Windows Plugin database security unavailable")
    if _WINDOWS_FILE_SECURITY is not None:
        _WINDOWS_FILE_SECURITY.harden_directory(path.parent)
    else:
        try:
            os.chmod(path.parent, 0o700, follow_symlinks=False)
        except (NotImplementedError, TypeError):
            os.chmod(path.parent, 0o700)
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and (not stat.S_ISREG(before.st_mode) or int(before.st_nlink) != 1):
        raise ValueError("unsafe Plugin database path")
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or int(opened.st_nlink) != 1:
            raise ValueError("unsafe Plugin database path")
        if _WINDOWS_FILE_SECURITY is not None:
            _WINDOWS_FILE_SECURITY.harden_file(descriptor)
            _WINDOWS_FILE_SECURITY.verify_file(descriptor)
        elif hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        after = path.lstat()
        if (
            not stat.S_ISREG(after.st_mode) or int(after.st_nlink) != 1
            or (int(after.st_dev), int(after.st_ino)) != (int(opened.st_dev), int(opened.st_ino))
        ):
            raise ValueError("unsafe Plugin database path")
    finally:
        os.close(descriptor)
    return _DatabaseIdentity(int(after.st_dev), int(after.st_ino))


def _verify_private_database(path: Path, identity: _DatabaseIdentity) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise ValueError("unsafe Plugin database path") from None
    if (
        not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1
        or (int(metadata.st_dev), int(metadata.st_ino)) != (identity.device, identity.inode)
    ):
        raise ValueError("unsafe Plugin database path")
    if _WINDOWS_FILE_SECURITY is None:
        geteuid = getattr(os, "geteuid", None)
        if stat.S_IMODE(metadata.st_mode) != 0o600 or (
            callable(geteuid) and int(metadata.st_uid) != int(geteuid())
        ):
            raise ValueError("unsafe Plugin database path")


class PluginStore:
    """Seven-day, per-principal bounded store; no raw keys or requests persist."""

    def __init__(
        self, path: Path, *, clock: Callable[[], datetime] | None = None,
        waiter_timeout_seconds: float = 0.5,
        max_records_per_principal: int = MAX_IDEMPOTENCY_RECORDS_PER_PRINCIPAL,
    ) -> None:
        if not isinstance(path, Path):
            raise ValueError("invalid Plugin database path")
        if not 0.01 <= waiter_timeout_seconds <= 10:
            raise ValueError("invalid waiter timeout")
        if type(max_records_per_principal) is not int or not 1 <= max_records_per_principal <= 10_000:
            raise ValueError("invalid Plugin capacity")
        self._identity = _prepare_private_database(path)
        self._path = path
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._waiter_timeout = waiter_timeout_seconds
        self._max_records = max_records_per_principal
        self._condition = _condition(path)
        self._closed = False
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        _verify_private_database(self._path, self._identity)
        connection = sqlite3.connect(self._path, timeout=2.0, isolation_level=None)
        try:
            _verify_private_database(self._path, self._identity)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA secure_delete = ON")
            return connection
        except Exception:
            connection.close()
            raise

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            existing_objects = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if existing_objects and existing_objects != set(_SCHEMA_COLUMNS):
                raise RuntimeError("unsupported Plugin database schema")
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS plugin_schema (
                    version INTEGER PRIMARY KEY CHECK(version = 1)
                );
                INSERT OR IGNORE INTO plugin_schema(version) VALUES (1);
                CREATE TABLE IF NOT EXISTS idempotency_records (
                    principal TEXT NOT NULL,
                    route TEXT NOT NULL,
                    key_hash TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('inflight','complete')),
                    status_code INTEGER,
                    response_json BLOB,
                    owner_pid INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY(principal, route, key_hash)
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    principal TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    decision_json BLOB NOT NULL,
                    outcome_json BLOB,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY(principal, decision_id)
                );
                CREATE TABLE IF NOT EXISTS plugin_connections (
                    principal TEXT PRIMARY KEY,
                    last_seen_at TEXT,
                    capabilities_negotiated INTEGER NOT NULL DEFAULT 0 CHECK(capabilities_negotiated IN (0,1)),
                    last_sync_outcome TEXT,
                    last_sync_at TEXT
                );
                COMMIT;
                """
            )
            versions = connection.execute("SELECT version FROM plugin_schema").fetchall()
            if versions != [(1,)]:
                raise RuntimeError("unsupported Plugin database schema")
            _validate_schema(connection)
            indeterminate = canonical_json(
                problem("dependency_unavailable", "Required local service is unavailable.", True)
            )
            connection.execute("BEGIN IMMEDIATE")
            owners = connection.execute(
                "SELECT DISTINCT owner_pid FROM idempotency_records WHERE state='inflight'"
            ).fetchall()
            for (owner_pid,) in owners:
                if not _process_is_alive(owner_pid):
                    connection.execute(
                        "UPDATE idempotency_records SET state='complete',status_code=503,response_json=? WHERE state='inflight' AND owner_pid=?",
                        (indeterminate, owner_pid),
                    )
            connection.commit()
        finally:
            connection.close()

    def close(self) -> None:
        self._closed = True

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("Plugin clock is invalid")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _hash(principal: str, route: str, key: str) -> str:
        return hashlib.sha256(b"openusage-plugin-key-v1\0" + principal.encode() + b"\0" + route.encode() + b"\0" + key.encode()).hexdigest()

    @staticmethod
    def _digest(principal: str, route: str, projection: object) -> str:
        return hashlib.sha256(b"openusage-plugin-request-v1\0" + principal.encode() + b"\0" + route.encode() + b"\0" + canonical_json(projection)).hexdigest()

    def execute_idempotent(
        self, *, principal: str, route: str, key: str, projection: object,
        operation: Callable[
            [],
            tuple[int, dict[str, object]]
            | tuple[int, dict[str, object], _SQLMutation],
        ],
    ) -> tuple[int, dict[str, object]]:
        if self._closed or principal not in PRINCIPALS or type(route) is not str or not route.startswith("/plugin/v1/"):
            raise ValueError("invalid idempotency input")
        if type(key) is not str or IDEMPOTENCY_KEY_PATTERN.fullmatch(key) is None or not callable(operation):
            raise ValueError("invalid idempotency input")
        key_hash = self._hash(principal, route, key)
        digest = self._digest(principal, route, projection)
        owner = self._claim(principal, route, key_hash, digest)
        if not owner:
            return self._await_result(principal, route, key_hash, digest)
        try:
            result = operation()
            if type(result) is not tuple or len(result) not in (2, 3):
                raise RuntimeError("invalid idempotent operation result")
            status, response = result[:2]
            mutation = result[2] if len(result) == 3 else None
            if type(status) is not int or not 100 <= status <= 599 or type(response) is not dict:
                raise RuntimeError("invalid idempotent operation result")
        except Exception:
            return self._complete_owner_failure(
                principal, route, key_hash, digest
            )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if mutation is not None:
                    if not callable(mutation):
                        raise RuntimeError("invalid idempotent mutation")
                    override = mutation(connection)
                    if override is not None:
                        if type(override) is not tuple or len(override) != 2:
                            raise RuntimeError("invalid idempotent mutation")
                        status, response = override
                        if type(status) is not int or type(response) is not dict:
                            raise RuntimeError("invalid idempotent mutation")
                encoded = canonical_json(response)
            except Exception:
                connection.rollback()
                connection.close()
                return self._complete_owner_failure(
                    principal, route, key_hash, digest
                )
            cursor = connection.execute(
                "UPDATE idempotency_records SET state='complete',status_code=?,response_json=? WHERE principal=? AND route=? AND key_hash=? AND request_digest=? AND state='inflight'",
                (status, encoded, principal, route, key_hash, digest),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("idempotency ownership was lost")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        with self._condition:
            self._condition.notify_all()
        return status, json.loads(encoded)

    def _complete_owner_failure(
        self, principal: str, route: str, key_hash: str, digest: str,
    ) -> tuple[int, dict[str, object]]:
        status = 500
        response = problem("internal_error", "Request could not be completed.", True)
        encoded = canonical_json(response)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE idempotency_records SET state='complete',status_code=?,response_json=? WHERE principal=? AND route=? AND key_hash=? AND request_digest=? AND state='inflight'",
                (status, encoded, principal, route, key_hash, digest),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT status_code,response_json FROM idempotency_records WHERE principal=? AND route=? AND key_hash=? AND request_digest=? AND state='complete'",
                    (principal, route, key_hash, digest),
                ).fetchone()
                if row is None:
                    raise RuntimeError("idempotency ownership was lost")
                status, encoded = int(row[0]), bytes(row[1])
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        with self._condition:
            self._condition.notify_all()
        return status, json.loads(encoded)

    def _claim(self, principal: str, route: str, key_hash: str, digest: str) -> bool:
        now = self._now()
        expires = now + timedelta(seconds=IDEMPOTENCY_RETENTION_SECONDS)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = utc_fixed6(now)
            connection.execute(
                "DELETE FROM idempotency_records WHERE principal=? AND state='complete' AND expires_at<=?",
                (principal, timestamp),
            )
            connection.execute(
                "DELETE FROM decisions WHERE principal=? AND expires_at<=?",
                (principal, timestamp),
            )
            row = connection.execute(
                "SELECT request_digest,state,status_code,response_json FROM idempotency_records WHERE principal=? AND route=? AND key_hash=?",
                (principal, route, key_hash),
            ).fetchone()
            if row is not None:
                connection.commit()
                if row[0] != digest:
                    raise IdempotencyConflict()
                if row[1] == "complete":
                    return False
                return False
            count = connection.execute(
                "SELECT COUNT(*) FROM idempotency_records WHERE principal=?", (principal,)
            ).fetchone()[0]
            if count >= self._max_records:
                connection.rollback()
                raise IdempotencyCapacityExceeded()
            connection.execute(
                "INSERT INTO idempotency_records(principal,route,key_hash,request_digest,state,status_code,response_json,owner_pid,created_at,expires_at) VALUES(?,?,?,?, 'inflight',NULL,NULL,?,?,?)",
                (principal, route, key_hash, digest, os.getpid(), utc_fixed6(now), utc_fixed6(expires)),
            )
            connection.commit()
            return True
        except (IdempotencyConflict, IdempotencyCapacityExceeded):
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _await_result(self, principal: str, route: str, key_hash: str, digest: str) -> tuple[int, dict[str, object]]:
        deadline = time.monotonic() + self._waiter_timeout
        while True:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT request_digest,state,status_code,response_json FROM idempotency_records WHERE principal=? AND route=? AND key_hash=?",
                    (principal, route, key_hash),
                ).fetchone()
            finally:
                connection.close()
            if row is None:
                raise RuntimeError("idempotency record disappeared")
            if row[0] != digest:
                raise IdempotencyConflict()
            if row[1] == "complete":
                return int(row[2]), json.loads(bytes(row[3]))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IdempotencyInProgress()
            with self._condition:
                self._condition.wait(min(remaining, 0.025))

    def insert_decision_in_transaction(
        self, connection: object, principal: str, decision: dict[str, object]
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise ValueError("invalid Plugin transaction")
        decision_id = decision.get("decisionId")
        if principal not in PRINCIPALS or type(decision_id) is not str or DECISION_ID_PATTERN.fullmatch(decision_id) is None:
            raise ValueError("invalid decision")
        now = self._now()
        connection.execute(
            "INSERT INTO decisions(principal,decision_id,decision_json,outcome_json,created_at,expires_at) VALUES(?,?,?,NULL,?,?)",
            (
                principal, decision_id, canonical_json(decision), utc_fixed6(now),
                utc_fixed6(now + timedelta(seconds=IDEMPOTENCY_RETENTION_SECONDS)),
            ),
        )

    def get_decision(self, principal: str, decision_id: str) -> tuple[dict[str, object], dict[str, object] | None]:
        if principal not in PRINCIPALS or type(decision_id) is not str or DECISION_ID_PATTERN.fullmatch(decision_id) is None:
            raise DecisionNotFound()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT decision_json,outcome_json FROM decisions WHERE principal=? AND decision_id=? AND expires_at>?",
                (principal, decision_id, utc_fixed6(self._now())),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise DecisionNotFound()
        return json.loads(bytes(row[0])), None if row[1] is None else json.loads(bytes(row[1]))

    def record_outcome_in_transaction(
        self, connection: object, principal: str, decision_id: str,
        receipt: dict[str, object],
    ) -> dict[str, object]:
        if not isinstance(connection, sqlite3.Connection):
            raise ValueError("invalid Plugin transaction")
        row = connection.execute(
            "SELECT outcome_json FROM decisions WHERE principal=? AND decision_id=? AND expires_at>?",
            (principal, decision_id, utc_fixed6(self._now())),
        ).fetchone()
        if row is None:
            raise DecisionNotFound()
        if row[0] is not None:
            existing = json.loads(bytes(row[0]))
            if existing.get("outcome") == receipt.get("outcome"):
                return existing
            raise DecisionOutcomeConflict()
        encoded = canonical_json(receipt)
        connection.execute(
            "UPDATE decisions SET outcome_json=? WHERE principal=? AND decision_id=? AND outcome_json IS NULL",
            (encoded, principal, decision_id),
        )
        return json.loads(encoded)

    def observe_principal(
        self, principal: str, *, negotiated: bool = False,
        outcome: str | None = None,
    ) -> None:
        if principal not in PRINCIPALS:
            return
        observed = utc_fixed6(self._now())
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO plugin_connections(principal,last_seen_at,capabilities_negotiated,last_sync_outcome,last_sync_at) VALUES(?,?,?,?,?) ON CONFLICT(principal) DO UPDATE SET last_seen_at=excluded.last_seen_at,capabilities_negotiated=MAX(plugin_connections.capabilities_negotiated,excluded.capabilities_negotiated),last_sync_outcome=COALESCE(excluded.last_sync_outcome,plugin_connections.last_sync_outcome),last_sync_at=CASE WHEN excluded.last_sync_outcome IS NULL THEN plugin_connections.last_sync_at ELSE excluded.last_sync_at END",
                (principal, observed, int(negotiated), outcome, observed if outcome is not None else None),
            )
        finally:
            connection.close()

    def connection_state(self, principal: str) -> dict[str, object]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT last_seen_at,capabilities_negotiated,last_sync_outcome,last_sync_at FROM plugin_connections WHERE principal=?", (principal,)
            ).fetchone()
        finally:
            connection.close()
        return {"lastSeenAt": None, "capabilitiesNegotiated": False, "lastSyncOutcome": "never", "lastSyncAt": None} if row is None else {
            "lastSeenAt": row[0], "capabilitiesNegotiated": bool(row[1]),
            "lastSyncOutcome": row[2] or "never", "lastSyncAt": row[3]
        }


def _validate_schema(connection: sqlite3.Connection) -> None:
    objects = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if objects != set(_SCHEMA_COLUMNS):
        raise RuntimeError("unsupported Plugin database schema")
    for table, expected in _SCHEMA_COLUMNS.items():
        columns = tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))
        if columns != expected:
            raise RuntimeError("unsupported Plugin database schema")
