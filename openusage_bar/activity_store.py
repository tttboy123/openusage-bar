from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .provider_catalog import catalog
from .activity_records import (
    ChangeRecord,
    ChangeSnapshot,
    DailyCostRecord,
    DailyCostRow,
    DailyCostSnapshot,
    DailyUsageRecord,
    DailyUsageRow,
    DailyUsageSnapshot,
    ProviderInstance,
    ProviderInstanceSnapshot,
    PurgeResult,
    QuotaHistorySnapshot,
    QuotaObservation,
    QuotaSnapshot,
    QuotaState,
    QuotaStateSnapshot,
    SourceStatus,
    SourceStatusSnapshot,
    UsageSummary,
    canonical_timestamp as _timestamp,
    validate_day as _validate_day,
    validate_id as _validate_id,
)
from .activity_schema import (
    DAILY_ACTIVITY_SOURCE_ID,
    EXPECTED_AUTOINCREMENT as _EXPECTED_AUTOINCREMENT,
    EXPECTED_INDEXES as _EXPECTED_INDEXES,
    EXPECTED_SCHEMA as _EXPECTED_SCHEMA,
    LEGACY_SOURCE_SCHEMAS as _LEGACY_SOURCE_SCHEMAS,
    SCHEMA_VERSION,
)

def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


class ActivityStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path, timeout=5.0, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        try:
            with self._lock:
                self._connection.execute("PRAGMA foreign_keys=ON")
                if self.path != ":memory:":
                    self._connection.execute("PRAGMA journal_mode=WAL")
                version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
                if version > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"database uses newer schema version {version}; supported version is {SCHEMA_VERSION}"
                    )
                self._validate_existing_schema(
                    require_all=version == SCHEMA_VERSION,
                    optional_missing=frozenset(),
                    allow_legacy_source_columns=version < SCHEMA_VERSION,
                )
                self._migrate_source_provenance()
                try:
                    self._initialize_schema()
                except sqlite3.IntegrityError as error:
                    raise RuntimeError(
                        "incompatible schema: change_log revisions are not unique"
                    ) from error
                self._validate_existing_schema(require_all=True)
                self._validate_required_indexes()
                with self._connection:
                    self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except Exception:
            self._connection.close()
            raise

    def _validate_existing_schema(
        self,
        *,
        require_all: bool = False,
        optional_missing: frozenset[str] = frozenset(),
        allow_legacy_source_columns: bool = False,
    ) -> None:
        existing = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        for table in sorted(existing & _EXPECTED_SCHEMA.keys()):
            actual = tuple(
                (
                    str(row[1]),
                    str(row[2]).upper(),
                    int(row[3]),
                    row[4],
                    int(row[5]),
                )
                for row in self._connection.execute(f"PRAGMA table_info({table})")
            )
            legacy = _LEGACY_SOURCE_SCHEMAS.get(table)
            if actual != _EXPECTED_SCHEMA[table] and not (
                allow_legacy_source_columns and legacy is not None and actual == legacy
            ):
                raise RuntimeError(
                    f"incompatible schema for {table}: table signature does not match"
                )
        if "change_log" in existing:
            duplicate = self._connection.execute(
                "SELECT record_type,record_id,revision FROM change_log "
                "GROUP BY record_type,record_id,revision HAVING COUNT(*)>1 LIMIT 1"
            ).fetchone()
            if duplicate is not None:
                raise RuntimeError(
                    "incompatible schema: change_log contains duplicate record revisions"
                )
        for table, column in _EXPECTED_AUTOINCREMENT.items():
            if table not in existing:
                continue
            sql_row = self._connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            sql = "" if sql_row is None or sql_row[0] is None else str(sql_row[0])
            normalized = re.sub(r"\s+", " ", sql.upper())
            identifier = re.escape(column.upper())
            pattern = (
                rf"(?:\(|,)\s*[\"`\[]?{identifier}[\"`\]]?\s+INTEGER\s+"
                rf"PRIMARY\s+KEY\s+AUTOINCREMENT(?:\s|,|\))"
            )
            if re.search(pattern, normalized) is None:
                raise RuntimeError(
                    f"incompatible schema for {table}: {column} must use INTEGER PRIMARY KEY AUTOINCREMENT"
                )
        if require_all:
            missing = sorted(_EXPECTED_SCHEMA.keys() - existing - optional_missing)
            if missing:
                raise RuntimeError(
                    f"incompatible schema: required tables are missing: {', '.join(missing)}"
                )

    def _migrate_source_provenance(self) -> None:
        existing = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        with self._connection:
            for table in ("daily_model_usage", "daily_coverage"):
                if table not in existing:
                    continue
                columns = {
                    str(row[1])
                    for row in self._connection.execute(f"PRAGMA table_info({table})")
                }
                if "source_id" not in columns:
                    self._connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN source_id TEXT NOT NULL DEFAULT 'legacy'"
                    )

    def _validate_required_indexes(self) -> None:
        for index_name, (table, expected_unique, expected_terms) in _EXPECTED_INDEXES.items():
            indexes = {
                str(row[1]): int(row[2])
                for row in self._connection.execute(f"PRAGMA index_list({table})")
            }
            if indexes.get(index_name) != expected_unique:
                raise RuntimeError(
                    f"incompatible schema for {table}: required index {index_name} is missing or invalid"
                )
            terms = tuple(
                (str(row[2]), int(row[3]))
                for row in self._connection.execute(f"PRAGMA index_xinfo({index_name})")
                if int(row[5]) == 1
            )
            if terms != expected_terms:
                raise RuntimeError(
                    f"incompatible schema for {table}: index {index_name} terms or sort direction do not match"
                )

    def _initialize_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS daily_costs(
            day TEXT NOT NULL, provider_id TEXT NOT NULL, account_ref TEXT NOT NULL DEFAULT '',
            cost_kind TEXT NOT NULL, currency TEXT NOT NULL, amount TEXT NOT NULL,
            basis TEXT NOT NULL, quality TEXT NOT NULL, imported_at TEXT NOT NULL,
            revision INTEGER NOT NULL, payload_hash TEXT NOT NULL,
            PRIMARY KEY(day,provider_id,account_ref,cost_kind,currency));
        CREATE TABLE IF NOT EXISTS daily_cost_coverage(
            day TEXT NOT NULL, provider_id TEXT NOT NULL, account_ref TEXT NOT NULL DEFAULT '',
            imported_at TEXT NOT NULL,
            PRIMARY KEY(day,provider_id,account_ref));
        CREATE INDEX IF NOT EXISTS daily_cost_provider_account_day
            ON daily_costs(provider_id, account_ref, day);
        CREATE TABLE IF NOT EXISTS daily_model_usage(
            day TEXT NOT NULL, provider_id TEXT NOT NULL, account_ref TEXT NOT NULL DEFAULT '',
            model_id TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
            cache_read_tokens INTEGER NOT NULL, cache_creation_tokens INTEGER NOT NULL,
            reasoning_tokens INTEGER, total_tokens INTEGER NOT NULL, cost_amount TEXT,
            cost_currency TEXT, cost_basis TEXT, quality TEXT NOT NULL, imported_at TEXT NOT NULL,
            revision INTEGER NOT NULL, payload_hash TEXT NOT NULL,
            source_id TEXT NOT NULL DEFAULT 'legacy',
            PRIMARY KEY(day,provider_id,account_ref,model_id));
        CREATE TABLE IF NOT EXISTS daily_coverage(
            day TEXT NOT NULL, provider_id TEXT NOT NULL, account_ref TEXT NOT NULL DEFAULT '',
            imported_at TEXT NOT NULL, source_id TEXT NOT NULL DEFAULT 'legacy',
            PRIMARY KEY(day,provider_id,account_ref));
        CREATE TABLE IF NOT EXISTS quota_state(
            record_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, provider_id TEXT NOT NULL,
            account_ref TEXT NOT NULL DEFAULT '', quota_name TEXT NOT NULL, unit TEXT NOT NULL, used TEXT,
            quota_limit TEXT, remaining TEXT, remaining_ratio REAL, resets_at TEXT,
            period_start TEXT, period_end TEXT, state TEXT NOT NULL, quality TEXT NOT NULL,
            stale INTEGER NOT NULL, revision INTEGER NOT NULL, payload_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS quota_snapshots(
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL,
            observed_at TEXT NOT NULL, provider_id TEXT NOT NULL,
            account_ref TEXT NOT NULL DEFAULT '', quota_name TEXT NOT NULL,
            payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS quota_snapshot_record_time
            ON quota_snapshots(record_id, observed_at DESC);
        CREATE INDEX IF NOT EXISTS quota_snapshot_provider_account_time
            ON quota_snapshots(provider_id, account_ref, observed_at DESC, snapshot_id DESC);
        CREATE TABLE IF NOT EXISTS source_status(
            provider_id TEXT NOT NULL, source_id TEXT NOT NULL, state TEXT NOT NULL,
            last_attempt_at TEXT NOT NULL,
            last_success_at TEXT, stale_at TEXT, error_code TEXT,
            PRIMARY KEY(provider_id,source_id));
        CREATE TABLE IF NOT EXISTS provider_instances(
            provider_id TEXT PRIMARY KEY, family_id TEXT NOT NULL,
            display_name TEXT NOT NULL, category TEXT NOT NULL,
            credential_source TEXT NOT NULL, source_kind TEXT NOT NULL,
            observed_at TEXT NOT NULL, revision INTEGER NOT NULL,
            payload_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS change_log(
            change_seq INTEGER PRIMARY KEY AUTOINCREMENT, record_type TEXT NOT NULL,
            record_id TEXT NOT NULL, revision INTEGER NOT NULL, operation TEXT NOT NULL,
            changed_at TEXT NOT NULL, payload_json TEXT, payload_hash TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS change_log_record_revision_unique
            ON change_log(record_type,record_id,revision);
        CREATE TABLE IF NOT EXISTS ledger_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        """
        with self._connection:
            self._connection.executescript(schema)

    @contextmanager
    def _write_transaction(self):
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    @contextmanager
    def _read_snapshot(self):
        """Hold a short, consistent read transaction on this store connection."""
        with self._lock:
            self._connection.execute("BEGIN DEFERRED")
            try:
                yield
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def __enter__(self) -> ActivityStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @property
    def schema_version(self) -> int:
        with self._lock:
            return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    @property
    def journal_mode(self) -> str:
        with self._lock:
            return str(self._connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    @property
    def foreign_keys_enabled(self) -> bool:
        with self._lock:
            return bool(self._connection.execute("PRAGMA foreign_keys").fetchone()[0])

    @property
    def current_change_seq(self) -> int:
        with self._lock:
            return self._current_change_seq_locked()

    def high_water_cursor(self) -> int:
        """Return the canonical ledger high-water mark."""
        return self.current_change_seq

    def table_names(self) -> set[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            return {str(row[0]) for row in rows}

    @staticmethod
    def _daily_record_id(day: str, provider_id: str, account_ref: str, model_id: str) -> str:
        return f"daily:{day}:{provider_id}:{account_ref}:{model_id}"

    @staticmethod
    def _coverage_record_id(day: str, provider_id: str, account_ref: str) -> str:
        return f"coverage:{day}:{provider_id}:{account_ref}"

    @staticmethod
    def _cost_record_id(
        day: str, provider_id: str, account_ref: str, cost_kind: str, currency: str
    ) -> str:
        return f"cost:{day}:{provider_id}:{account_ref}:{cost_kind}:{currency}"

    @staticmethod
    def _cost_coverage_record_id(day: str, provider_id: str, account_ref: str) -> str:
        return f"cost-coverage:{day}:{provider_id}:{account_ref}"

    @staticmethod
    def _daily_payload(row: DailyUsageRow, source_id: str) -> tuple[str, str]:
        payload = asdict(row)
        payload.pop("imported_at")
        payload["source_id"] = source_id
        payload_json = _json(payload)
        return payload_json, _hash(payload_json)

    @staticmethod
    def _cost_payload(row: DailyCostRow) -> tuple[str, str]:
        payload = asdict(row)
        payload.pop("imported_at")
        payload_json = _json(payload)
        return payload_json, _hash(payload_json)

    @staticmethod
    def _provider_instance_payload(instance: ProviderInstance) -> tuple[str, str]:
        # Observation time and store-owned revision are operational metadata, not
        # provider identity. Keeping them out makes repeated discovery a no-op.
        payload = asdict(instance)
        payload.pop("observed_at")
        payload.pop("revision")
        payload_json = _json(payload)
        return payload_json, _hash(payload_json)

    @staticmethod
    def _validate_provider_family_binding(instance: ProviderInstance) -> None:
        known_ids = frozenset(catalog.family_ids)
        if instance.family_id not in known_ids:
            if instance.provider_id != instance.family_id:
                raise ValueError(
                    "unknown family may bind only to an identical provider_id"
                )
            source_pair = (instance.credential_source, instance.source_kind)
            valid_openusage = (
                source_pair == ("openusage", "openusage")
                and instance.category == "api"
            )
            valid_generic_https = (
                source_pair == ("api_key", "generic_https")
                and instance.category in {"api", "subscription"}
            )
            if not (valid_openusage or valid_generic_https):
                raise ValueError(
                    "unknown family must use an explicit safe source contract"
                )
            return

        if instance.provider_id in known_ids and instance.provider_id != instance.family_id:
            raise ValueError("known provider_id and family_id have a family mismatch")

        family = catalog.require(instance.family_id)
        if instance.category != family.category:
            raise ValueError("provider instance category does not match its family")
        if (
            instance.credential_source == "api_key"
            and instance.source_kind == "generic_https"
            and instance.provider_id != instance.family_id
        ):
            return
        matching_sources = tuple(
            source
            for source in family.sources
            if source.source_id == instance.credential_source
        )
        if not matching_sources:
            raise ValueError("credential_source is not declared by the provider family")
        if matching_sources[0].kind != instance.source_kind:
            raise ValueError("source_kind does not match the provider family source")

    def _append_change(
        self,
        record_type: str,
        record_id: str,
        revision: int,
        operation: str,
        changed_at: str,
        payload_json: str | None,
        payload_hash: str,
    ) -> None:
        self._connection.execute(
            "INSERT INTO change_log(record_type,record_id,revision,operation,changed_at,payload_json,payload_hash) VALUES(?,?,?,?,?,?,?)",
            (record_type, record_id, revision, operation, changed_at, payload_json, payload_hash),
        )

    def _next_revision_locked(self, record_type: str, record_id: str) -> int:
        latest = self._connection.execute(
            "SELECT COALESCE(MAX(revision),0) FROM change_log WHERE record_type=? AND record_id=?",
            (record_type, record_id),
        ).fetchone()[0]
        return int(latest) + 1

    def upsert_provider_instance(self, instance: ProviderInstance) -> ProviderInstance:
        if not isinstance(instance, ProviderInstance):
            raise TypeError("instance must be a ProviderInstance")
        self._validate_provider_family_binding(instance)
        payload_json, payload_hash = self._provider_instance_payload(instance)
        with self._write_transaction():
            old = self._connection.execute(
                "SELECT * FROM provider_instances WHERE provider_id=?",
                (instance.provider_id,),
            ).fetchone()
            if old is not None and str(old["family_id"]) != instance.family_id:
                raise ValueError("provider instance has a family mismatch")
            if old is not None and str(old["payload_hash"]) == payload_hash:
                return self._row_to_provider_instance(old)

            revision = self._next_revision_locked(
                "provider_instance", instance.provider_id
            )
            self._connection.execute(
                "INSERT INTO provider_instances("
                "provider_id,family_id,display_name,category,credential_source,"
                "source_kind,observed_at,revision,payload_hash"
                ") VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(provider_id) DO UPDATE SET "
                "family_id=excluded.family_id,display_name=excluded.display_name,"
                "category=excluded.category,credential_source=excluded.credential_source,"
                "source_kind=excluded.source_kind,observed_at=excluded.observed_at,"
                "revision=excluded.revision,payload_hash=excluded.payload_hash",
                (
                    instance.provider_id,
                    instance.family_id,
                    instance.display_name,
                    instance.category,
                    instance.credential_source,
                    instance.source_kind,
                    instance.observed_at,
                    revision,
                    payload_hash,
                ),
            )
            self._append_change(
                "provider_instance",
                instance.provider_id,
                revision,
                "upsert",
                instance.observed_at,
                payload_json,
                payload_hash,
            )
            row = self._connection.execute(
                "SELECT * FROM provider_instances WHERE provider_id=?",
                (instance.provider_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - protected by the transaction
                raise RuntimeError("provider instance upsert did not persist a row")
            return self._row_to_provider_instance(row)

    @staticmethod
    def _row_to_provider_instance(row: sqlite3.Row) -> ProviderInstance:
        values = dict(row)
        values.pop("payload_hash")
        return ProviderInstance(**values)

    def provider_instances(self) -> tuple[ProviderInstance, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM provider_instances ORDER BY provider_id"
            ).fetchall()
            return tuple(self._row_to_provider_instance(row) for row in rows)

    def snapshot_provider_instances(self) -> ProviderInstanceSnapshot:
        with self._read_snapshot():
            rows = self._connection.execute(
                "SELECT * FROM provider_instances ORDER BY provider_id"
            ).fetchall()
            return ProviderInstanceSnapshot(
                tuple(self._row_to_provider_instance(row) for row in rows),
                self._current_change_seq_locked(),
            )

    def replace_daily_costs(
        self,
        provider_id: str,
        day: str,
        rows: Iterable[DailyCostRow],
        *,
        account_ref: str = "",
        imported_at: str | None = None,
    ) -> None:
        _validate_id("provider_id", provider_id)
        _validate_day(day)
        if account_ref:
            _validate_id("account_ref", account_ref)
        materialized = list(rows)
        for row in materialized:
            if (row.provider_id, row.account_ref, row.day) != (
                provider_id, account_ref, day
            ):
                raise ValueError("daily cost row does not match replacement scope")
        marker_time = _timestamp(
            imported_at
            or (materialized[0].imported_at if materialized else datetime.now(timezone.utc).isoformat()),
            "imported_at",
        )
        with self._write_transaction():
            self._replace_daily_costs_locked(
                provider_id, day, materialized, account_ref, marker_time
            )

    def _replace_daily_costs_locked(
        self,
        provider_id: str,
        day: str,
        materialized: list[DailyCostRow],
        account_ref: str,
        marker_time: str,
    ) -> None:
        existing_rows = self._connection.execute(
            "SELECT cost_kind,currency,revision,payload_hash FROM daily_costs "
            "WHERE day=? AND provider_id=? AND account_ref=?",
            (day, provider_id, account_ref),
        ).fetchall()
        existing = {
            (str(row["cost_kind"]), str(row["currency"])): row
            for row in existing_rows
        }
        incoming = {(row.cost_kind, row.currency): row for row in materialized}
        if len(incoming) != len(materialized):
            raise ValueError("replacement contains duplicate cost identity")

        for cost_kind, currency in sorted(existing.keys() - incoming.keys()):
            old = existing[(cost_kind, currency)]
            record_id = self._cost_record_id(
                day, provider_id, account_ref, cost_kind, currency
            )
            revision = self._next_revision_locked("daily_cost", record_id)
            self._connection.execute(
                "DELETE FROM daily_costs WHERE day=? AND provider_id=? AND account_ref=? "
                "AND cost_kind=? AND currency=?",
                (day, provider_id, account_ref, cost_kind, currency),
            )
            self._append_change(
                "daily_cost", record_id, revision, "delete", marker_time,
                None, str(old["payload_hash"]),
            )

        columns = (
            "day,provider_id,account_ref,cost_kind,currency,amount,basis,quality,"
            "imported_at,revision,payload_hash"
        )
        for identity in sorted(incoming):
            item = incoming[identity]
            payload_json, payload_hash = self._cost_payload(item)
            old = existing.get(identity)
            if old is not None and old["payload_hash"] == payload_hash:
                continue
            record_id = self._cost_record_id(
                item.day, item.provider_id, item.account_ref,
                item.cost_kind, item.currency,
            )
            revision = self._next_revision_locked("daily_cost", record_id)
            values = (
                item.day, item.provider_id, item.account_ref, item.cost_kind,
                item.currency, item.amount, item.basis, item.quality,
                item.imported_at, revision, payload_hash,
            )
            self._connection.execute(
                f"INSERT OR REPLACE INTO daily_costs({columns}) "
                f"VALUES({','.join('?' for _ in values)})",
                values,
            )
            self._append_change(
                "daily_cost", record_id, revision,
                "insert" if old is None else "update", item.imported_at,
                payload_json, payload_hash,
            )

        coverage = self._connection.execute(
            "SELECT 1 FROM daily_cost_coverage "
            "WHERE day=? AND provider_id=? AND account_ref=?",
            (day, provider_id, account_ref),
        ).fetchone()
        self._connection.execute(
            "INSERT OR REPLACE INTO daily_cost_coverage"
            "(day,provider_id,account_ref,imported_at) VALUES(?,?,?,?)",
            (day, provider_id, account_ref, marker_time),
        )
        if coverage is None:
            record_id = self._cost_coverage_record_id(day, provider_id, account_ref)
            payload_json = _json(
                {"account_ref": account_ref, "day": day, "provider_id": provider_id}
            )
            self._append_change(
                "daily_cost_coverage", record_id,
                self._next_revision_locked("daily_cost_coverage", record_id),
                "insert", marker_time, payload_json, _hash(payload_json),
            )

    def replace_daily_cost_range(
        self,
        provider_id: str,
        since: date,
        until: date,
        rows: Iterable[DailyCostRow],
        *,
        account_ref: str = "",
        imported_at: str | None = None,
    ) -> None:
        _validate_id("provider_id", provider_id)
        if account_ref:
            _validate_id("account_ref", account_ref)
        if (
            not isinstance(since, date)
            or isinstance(since, datetime)
            or not isinstance(until, date)
            or isinstance(until, datetime)
            or since > until
        ):
            raise ValueError("replacement range must use ordered dates")
        materialized = list(rows)
        grouped: dict[str, list[DailyCostRow]] = {}
        for row in materialized:
            row_day = date.fromisoformat(row.day)
            if (
                row.provider_id != provider_id
                or row.account_ref != account_ref
                or not since <= row_day <= until
            ):
                raise ValueError("daily cost row does not match replacement range")
            grouped.setdefault(row.day, []).append(row)
        marker_time = _timestamp(
            imported_at or datetime.now(timezone.utc).isoformat(), "imported_at"
        )
        with self._write_transaction():
            current = since
            while current <= until:
                day = current.isoformat()
                self._replace_daily_costs_locked(
                    provider_id, day, grouped.get(day, []), account_ref, marker_time
                )
                current += timedelta(days=1)

    def has_cost_history(self, provider_id: str, account_ref: str = "") -> bool:
        _validate_id("provider_id", provider_id)
        if account_ref:
            _validate_id("account_ref", account_ref)
        with self._lock:
            return self._connection.execute(
                "SELECT 1 FROM daily_cost_coverage "
                "WHERE provider_id=? AND account_ref=? LIMIT 1",
                (provider_id, account_ref),
            ).fetchone() is not None

    @staticmethod
    def _row_to_cost(
        row: sqlite3.Row, *, stored: bool
    ) -> DailyCostRow | DailyCostRecord:
        values = dict(row)
        if stored:
            values["record_id"] = ActivityStore._cost_record_id(
                values["day"], values["provider_id"], values["account_ref"],
                values["cost_kind"], values["currency"],
            )
            return DailyCostRecord(**values)
        values.pop("revision")
        values.pop("payload_hash")
        return DailyCostRow(**values)

    def daily_costs(self, start_day: str, end_day: str) -> list[DailyCostRow]:
        _validate_day(start_day)
        _validate_day(end_day)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM daily_costs WHERE day BETWEEN ? AND ? "
                "ORDER BY day,provider_id,account_ref,cost_kind,currency",
                (start_day, end_day),
            ).fetchall()
            return [self._row_to_cost(row, stored=False) for row in rows]  # type: ignore[misc]

    def snapshot_daily_costs(
        self, start_day: str, end_day: str
    ) -> DailyCostSnapshot:
        _validate_day(start_day)
        _validate_day(end_day)
        with self._read_snapshot():
            rows = self._connection.execute(
                "SELECT * FROM daily_costs WHERE day BETWEEN ? AND ? "
                "ORDER BY day,provider_id,account_ref,cost_kind,currency",
                (start_day, end_day),
            ).fetchall()
            coverage = self._connection.execute(
                "SELECT day,provider_id,account_ref FROM daily_cost_coverage "
                "WHERE day BETWEEN ? AND ?",
                (start_day, end_day),
            ).fetchall()
            known_scopes = self._connection.execute(
                "SELECT DISTINCT provider_id,account_ref FROM daily_cost_coverage "
                "UNION SELECT DISTINCT provider_id,account_ref FROM daily_costs "
                "ORDER BY provider_id,account_ref"
            ).fetchall()
            return DailyCostSnapshot(
                rows=tuple(self._row_to_cost(row, stored=True) for row in rows),  # type: ignore[misc]
                covered=frozenset(
                    (str(row[0]), str(row[1]), str(row[2])) for row in coverage
                ),
                known_scopes=frozenset(
                    (str(row[0]), str(row[1])) for row in known_scopes
                ),
                cursor=self._current_change_seq_locked(),
            )

    def replace_daily_usage(
        self,
        provider_id: str,
        day: str,
        rows: Iterable[DailyUsageRow],
        *,
        account_ref: str = "",
        imported_at: str | None = None,
        source_id: str = DAILY_ACTIVITY_SOURCE_ID,
    ) -> None:
        _validate_id("provider_id", provider_id)
        _validate_id("source_id", source_id)
        _validate_day(day)
        if account_ref:
            _validate_id("account_ref", account_ref)
        materialized = list(rows)
        for row in materialized:
            if (row.provider_id, row.account_ref, row.day) != (provider_id, account_ref, day):
                raise ValueError("daily usage row does not match replacement scope")
        marker_time = _timestamp(
            imported_at or (materialized[0].imported_at if materialized else datetime.now(timezone.utc).isoformat()),
            "imported_at",
        )
        with self._write_transaction():
            self._replace_daily_usage_locked(
                provider_id, day, materialized, account_ref, marker_time, source_id
            )

    def _replace_daily_usage_locked(
        self,
        provider_id: str,
        day: str,
        materialized: list[DailyUsageRow],
        account_ref: str,
        marker_time: str,
        source_id: str,
    ) -> None:
        existing_rows = self._connection.execute(
            "SELECT model_id,revision,payload_hash,source_id FROM daily_model_usage WHERE day=? AND provider_id=? AND account_ref=?",
            (day, provider_id, account_ref),
        ).fetchall()
        existing = {str(row["model_id"]): row for row in existing_rows}
        incoming = {row.model_id: row for row in materialized}
        if len(incoming) != len(materialized):
            raise ValueError("replacement contains duplicate model_id")

        for model_id in sorted(existing.keys() - incoming.keys()):
            old = existing[model_id]
            record_id = self._daily_record_id(day, provider_id, account_ref, model_id)
            revision = self._next_revision_locked("daily_usage", record_id)
            self._connection.execute(
                "DELETE FROM daily_model_usage WHERE day=? AND provider_id=? AND account_ref=? AND model_id=?",
                (day, provider_id, account_ref, model_id),
            )
            self._append_change(
                "daily_usage",
                record_id,
                revision,
                "delete",
                marker_time,
                None,
                str(old["payload_hash"]),
            )

        columns = (
            "day,provider_id,account_ref,model_id,input_tokens,output_tokens,cache_read_tokens,"
            "cache_creation_tokens,reasoning_tokens,total_tokens,cost_amount,cost_currency,cost_basis,"
            "quality,imported_at,revision,payload_hash,source_id"
        )
        for model_id in sorted(incoming):
            item = incoming[model_id]
            payload_json, payload_hash = self._daily_payload(item, source_id)
            old = existing.get(model_id)
            if old is not None and old["payload_hash"] == payload_hash:
                continue
            record_id = self._daily_record_id(day, provider_id, account_ref, model_id)
            revision = self._next_revision_locked("daily_usage", record_id)
            values = (
                item.day,
                item.provider_id,
                item.account_ref,
                item.model_id,
                item.input_tokens,
                item.output_tokens,
                item.cache_read_tokens,
                item.cache_creation_tokens,
                item.reasoning_tokens,
                item.total_tokens,
                item.cost_amount,
                item.cost_currency,
                item.cost_basis,
                item.quality,
                item.imported_at,
                revision,
                payload_hash,
                source_id,
            )
            self._connection.execute(
                f"INSERT OR REPLACE INTO daily_model_usage({columns}) VALUES({','.join('?' for _ in values)})",
                values,
            )
            self._append_change(
                "daily_usage",
                record_id,
                revision,
                "insert" if old is None else "update",
                item.imported_at,
                payload_json,
                payload_hash,
            )

        coverage = self._connection.execute(
            "SELECT source_id FROM daily_coverage WHERE day=? AND provider_id=? AND account_ref=?",
            (day, provider_id, account_ref),
        ).fetchone()
        self._connection.execute(
            "INSERT OR REPLACE INTO daily_coverage(day,provider_id,account_ref,imported_at,source_id) VALUES(?,?,?,?,?)",
            (day, provider_id, account_ref, marker_time, source_id),
        )
        if coverage is None or str(coverage["source_id"]) != source_id:
            record_id = self._coverage_record_id(day, provider_id, account_ref)
            payload_json = _json(
                {
                    "account_ref": account_ref,
                    "day": day,
                    "provider_id": provider_id,
                    "source_id": source_id,
                }
            )
            self._append_change(
                "daily_coverage",
                record_id,
                self._next_revision_locked("daily_coverage", record_id),
                "insert" if coverage is None else "update",
                marker_time,
                payload_json,
                _hash(payload_json),
            )

    def has_daily_history(self, provider_id: str, account_ref: str = "") -> bool:
        _validate_id("provider_id", provider_id)
        if account_ref:
            _validate_id("account_ref", account_ref)
        with self._lock:
            return self._connection.execute(
                "SELECT 1 FROM daily_coverage WHERE provider_id=? AND account_ref=? LIMIT 1",
                (provider_id, account_ref),
            ).fetchone() is not None

    def replace_provider_days(
        self,
        provider_id: str,
        since: date,
        until: date,
        rows: Iterable[DailyUsageRow],
        *,
        account_ref: str = "",
        imported_at: str | None = None,
        source_id: str = DAILY_ACTIVITY_SOURCE_ID,
    ) -> None:
        _validate_id("provider_id", provider_id)
        _validate_id("source_id", source_id)
        if account_ref:
            _validate_id("account_ref", account_ref)
        if (
            not isinstance(since, date)
            or isinstance(since, datetime)
            or not isinstance(until, date)
            or isinstance(until, datetime)
            or since > until
        ):
            raise ValueError("replacement range must use ordered dates")
        materialized = list(rows)
        grouped: dict[str, list[DailyUsageRow]] = {}
        for row in materialized:
            row_day = date.fromisoformat(row.day)
            if (
                row.provider_id != provider_id
                or row.account_ref != account_ref
                or not since <= row_day <= until
            ):
                raise ValueError("daily usage row does not match replacement range")
            grouped.setdefault(row.day, []).append(row)
        marker_time = _timestamp(
            imported_at or datetime.now(timezone.utc).isoformat(), "imported_at"
        )
        with self._write_transaction():
            current = since
            while current <= until:
                day = current.isoformat()
                self._replace_daily_usage_locked(
                    provider_id, day, grouped.get(day, []), account_ref, marker_time,
                    source_id,
                )
                current += timedelta(days=1)

    def has_source_success(self, provider_id: str, source_id: str) -> bool:
        _validate_id("provider_id", provider_id)
        _validate_id("source_id", source_id)
        with self._lock:
            return self._connection.execute(
                "SELECT 1 FROM source_status WHERE provider_id=? AND source_id=? "
                "AND last_success_at IS NOT NULL LIMIT 1",
                (provider_id, source_id),
            ).fetchone() is not None

    def commit_usage_import_success(
        self,
        provider_id: str,
        source_id: str,
        since: date,
        until: date,
        rows: Iterable[DailyUsageRow],
        attempted_at: datetime,
        *,
        account_ref: str = "",
        freshness_seconds: int = 300,
    ) -> bool:
        _validate_id("provider_id", provider_id)
        _validate_id("source_id", source_id)
        if account_ref:
            _validate_id("account_ref", account_ref)
        if not isinstance(attempted_at, datetime) or attempted_at.tzinfo is None:
            raise ValueError("attempted_at must include a timezone")
        if since > until:
            raise ValueError("replacement range must use ordered dates")
        materialized = list(rows)
        grouped: dict[str, list[DailyUsageRow]] = {}
        for row in materialized:
            row_day = date.fromisoformat(row.day)
            if (
                row.provider_id != provider_id
                or row.account_ref != account_ref
                or not since <= row_day <= until
            ):
                raise ValueError("daily usage row does not match replacement range")
            grouped.setdefault(row.day, []).append(row)
        marker_time = _timestamp(attempted_at.isoformat(), "attempted_at")
        with self._write_transaction():
            if self._has_newer_source_attempt_locked(provider_id, source_id, marker_time):
                return False
            current = since
            while current <= until:
                day = current.isoformat()
                self._replace_daily_usage_locked(
                    provider_id, day, grouped.get(day, []), account_ref, marker_time,
                    source_id,
                )
                current += timedelta(days=1)
            self._record_source_success_locked(
                provider_id, source_id, attempted_at, freshness_seconds
            )
        return True

    def commit_cost_import_success(
        self,
        provider_id: str,
        source_id: str,
        since: date,
        until: date,
        rows: Iterable[DailyCostRow],
        attempted_at: datetime,
        *,
        account_ref: str = "",
        freshness_seconds: int = 300,
    ) -> bool:
        _validate_id("provider_id", provider_id)
        _validate_id("source_id", source_id)
        if account_ref:
            _validate_id("account_ref", account_ref)
        if not isinstance(attempted_at, datetime) or attempted_at.tzinfo is None:
            raise ValueError("attempted_at must include a timezone")
        if since > until:
            raise ValueError("replacement range must use ordered dates")
        materialized = list(rows)
        grouped: dict[str, list[DailyCostRow]] = {}
        for row in materialized:
            row_day = date.fromisoformat(row.day)
            if (
                row.provider_id != provider_id
                or row.account_ref != account_ref
                or not since <= row_day <= until
            ):
                raise ValueError("daily cost row does not match replacement range")
            grouped.setdefault(row.day, []).append(row)
        marker_time = _timestamp(attempted_at.isoformat(), "attempted_at")
        with self._write_transaction():
            if self._has_newer_source_attempt_locked(provider_id, source_id, marker_time):
                return False
            current = since
            while current <= until:
                day = current.isoformat()
                self._replace_daily_costs_locked(
                    provider_id, day, grouped.get(day, []), account_ref, marker_time
                )
                current += timedelta(days=1)
            self._record_source_success_locked(
                provider_id, source_id, attempted_at, freshness_seconds
            )
        return True

    def _has_newer_source_attempt_locked(
        self, provider_id: str, source_id: str, attempted: str
    ) -> bool:
        row = self._connection.execute(
            "SELECT last_attempt_at FROM source_status "
            "WHERE provider_id=? AND source_id=?",
            (provider_id, source_id),
        ).fetchone()
        return row is not None and str(row[0]) > attempted

    @staticmethod
    def _row_to_daily(row: sqlite3.Row, *, stored: bool) -> DailyUsageRow | DailyUsageRecord:
        values = dict(row)
        if stored:
            values["record_id"] = ActivityStore._daily_record_id(
                values["day"], values["provider_id"], values["account_ref"], values["model_id"]
            )
            return DailyUsageRecord(**values)
        values.pop("revision")
        values.pop("payload_hash")
        values.pop("source_id")
        return DailyUsageRow(**values)

    def daily_usage(self, start_day: str, end_day: str) -> list[DailyUsageRow]:
        _validate_day(start_day)
        _validate_day(end_day)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM daily_model_usage WHERE day BETWEEN ? AND ? ORDER BY day,provider_id,account_ref,model_id",
                (start_day, end_day),
            ).fetchall()
            return [self._row_to_daily(row, stored=False) for row in rows]  # type: ignore[misc]

    def is_day_covered(self, provider_id: str, day: str, account_ref: str = "") -> bool:
        _validate_id("provider_id", provider_id)
        _validate_day(day)
        if account_ref:
            _validate_id("account_ref", account_ref)
        with self._lock:
            return self._connection.execute(
                "SELECT 1 FROM daily_coverage WHERE day=? AND provider_id=? AND account_ref=?",
                (day, provider_id, account_ref),
            ).fetchone() is not None

    def snapshot_daily_usage(self, start_day: str, end_day: str) -> DailyUsageSnapshot:
        _validate_day(start_day)
        _validate_day(end_day)
        with self._lock:
            self._connection.execute("BEGIN DEFERRED")
            try:
                rows = self._connection.execute(
                    "SELECT * FROM daily_model_usage WHERE day BETWEEN ? AND ? ORDER BY day,provider_id,account_ref,model_id",
                    (start_day, end_day),
                ).fetchall()
                coverage = self._connection.execute(
                    "SELECT day,provider_id,account_ref,source_id FROM daily_coverage WHERE day BETWEEN ? AND ?",
                    (start_day, end_day),
                ).fetchall()
                known_scopes = self._connection.execute(
                    "SELECT DISTINCT provider_id,account_ref FROM daily_coverage "
                    "UNION SELECT DISTINCT provider_id,account_ref FROM daily_model_usage "
                    "UNION SELECT DISTINCT provider_id,'' AS account_ref FROM source_status "
                    "WHERE source_id=? "
                    "ORDER BY provider_id,account_ref",
                    (DAILY_ACTIVITY_SOURCE_ID,),
                ).fetchall()
                cursor = self._current_change_seq_locked()
                snapshot = DailyUsageSnapshot(
                    rows=tuple(self._row_to_daily(row, stored=True) for row in rows),  # type: ignore[misc]
                    covered=frozenset(
                        (str(row[0]), str(row[1]), str(row[2])) for row in coverage
                    ),
                    coverage_sources=frozenset(
                        (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
                        for row in coverage
                    ),
                    known_scopes=frozenset(
                        (str(row[0]), str(row[1])) for row in known_scopes
                    ),
                    cursor=cursor,
                )
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
                return snapshot

    @staticmethod
    def _quota_semantic_payload(observation: QuotaObservation) -> tuple[str, str]:
        payload = asdict(observation)
        payload.pop("observed_at")
        payload_json = _json(payload)
        return payload_json, _hash(payload_json)

    @staticmethod
    def _quota_snapshot_payload(observation: QuotaObservation) -> tuple[str, str]:
        payload_json = _json(asdict(observation))
        return payload_json, _hash(payload_json)

    def record_quota(self, observation: QuotaObservation) -> QuotaState:
        semantic_json, semantic_hash = self._quota_semantic_payload(observation)
        snapshot_json, snapshot_hash = self._quota_snapshot_payload(observation)
        with self._write_transaction():
            old = self._connection.execute(
                "SELECT * FROM quota_state WHERE record_id=?", (observation.record_id,)
            ).fetchone()
            if old is not None and observation.observed_at < str(old["observed_at"]):
                duplicate = self._connection.execute(
                    "SELECT 1 FROM quota_snapshots "
                    "WHERE record_id=? AND observed_at=? AND payload_hash=? LIMIT 1",
                    (observation.record_id, observation.observed_at, snapshot_hash),
                ).fetchone()
                if duplicate is None:
                    self._connection.execute(
                        "INSERT INTO quota_snapshots(record_id,observed_at,provider_id,account_ref,quota_name,payload_json,payload_hash) VALUES(?,?,?,?,?,?,?)",
                        (
                            observation.record_id,
                            observation.observed_at,
                            observation.provider_id,
                            observation.account_ref,
                            observation.quota_name,
                            snapshot_json,
                            snapshot_hash,
                        ),
                    )
                return self._quota_state_by_id_locked(observation.record_id)
            changed = old is None or old["payload_hash"] != semantic_hash
            revision = 1 if old is None else int(old["revision"]) + (1 if changed else 0)
            should_snapshot = changed
            if not changed:
                latest = self._connection.execute(
                    "SELECT observed_at FROM quota_snapshots WHERE record_id=? ORDER BY observed_at DESC LIMIT 1",
                    (observation.record_id,),
                ).fetchone()
                if latest is None:
                    should_snapshot = True
                else:
                    current_time = datetime.fromisoformat(observation.observed_at.replace("Z", "+00:00"))
                    previous_time = datetime.fromisoformat(str(latest[0]).replace("Z", "+00:00"))
                    should_snapshot = (current_time - previous_time).total_seconds() >= 3600

            values = asdict(observation)
            columns = list(values) + ["revision", "payload_hash"]
            self._connection.execute(
                f"INSERT OR REPLACE INTO quota_state({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                tuple(values.values()) + (revision, semantic_hash),
            )
            if should_snapshot:
                self._connection.execute(
                    "INSERT INTO quota_snapshots(record_id,observed_at,provider_id,account_ref,quota_name,payload_json,payload_hash) VALUES(?,?,?,?,?,?,?)",
                    (observation.record_id, observation.observed_at, observation.provider_id,
                     observation.account_ref, observation.quota_name, snapshot_json, snapshot_hash),
                )
            if changed:
                self._append_change(
                    "quota", observation.record_id, revision, "insert" if old is None else "update",
                    observation.observed_at, semantic_json, semantic_hash,
                )
            return self._quota_state_by_id_locked(observation.record_id)

    @staticmethod
    def _row_to_quota_state(row: sqlite3.Row) -> QuotaState:
        values = dict(row)
        values["stale"] = bool(values["stale"])
        return QuotaState(**values)

    def _quota_state_by_id_locked(self, record_id: str) -> QuotaState:
        row = self._connection.execute("SELECT * FROM quota_state WHERE record_id=?", (record_id,)).fetchone()
        if row is None:
            raise KeyError(record_id)
        return self._row_to_quota_state(row)

    def quota_states(self) -> list[QuotaState]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM quota_state ORDER BY record_id").fetchall()
            return [self._row_to_quota_state(row) for row in rows]

    def snapshot_quota_states(self) -> QuotaStateSnapshot:
        with self._read_snapshot():
            rows = self._connection.execute(
                "SELECT * FROM quota_state ORDER BY provider_id,account_ref,quota_name,record_id"
            ).fetchall()
            return QuotaStateSnapshot(
                tuple(self._row_to_quota_state(row) for row in rows),
                self._current_change_seq_locked(),
            )

    def quota_snapshots(self, record_id: str) -> list[QuotaSnapshot]:
        _validate_id("record_id", record_id)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM quota_snapshots WHERE record_id=? ORDER BY observed_at,snapshot_id",
                (record_id,),
            ).fetchall()
            return [QuotaSnapshot(**dict(row)) for row in rows]

    def snapshot_quota_history(
        self,
        *,
        provider_id: str | None = None,
        account_ref: str | None = None,
        from_time: str | None = None,
        to_time: str | None = None,
        limit: int = 1000,
    ) -> QuotaHistorySnapshot:
        if provider_id is not None:
            _validate_id("provider_id", provider_id)
        if account_ref is not None and account_ref:
            _validate_id("account_ref", account_ref)
        start = _timestamp(from_time, "from_time")
        end = _timestamp(to_time, "to_time")
        if start is not None and end is not None and start > end:
            raise ValueError("from_time must not be after to_time")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        clauses: list[str] = []
        arguments: list[str] = []
        if provider_id is not None:
            clauses.append("provider_id=?")
            arguments.append(provider_id)
        if account_ref is not None:
            clauses.append("account_ref=?")
            arguments.append(account_ref)
        if start is not None:
            clauses.append("observed_at>=?")
            arguments.append(start)
        if end is not None:
            clauses.append("observed_at<=?")
            arguments.append(end)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._read_snapshot():
            rows = self._connection.execute(
                "SELECT * FROM (SELECT * FROM quota_snapshots" + where
                + " ORDER BY observed_at DESC,snapshot_id DESC LIMIT ?) "
                "ORDER BY observed_at,provider_id,account_ref,quota_name,snapshot_id",
                [*arguments, limit],
            ).fetchall()
            return QuotaHistorySnapshot(
                tuple(QuotaSnapshot(**dict(row)) for row in rows),
                self._current_change_seq_locked(),
            )

    def record_source_success(
        self,
        provider_id: str,
        source_id: str,
        attempted_at: datetime,
        *,
        freshness_seconds: int = 300,
        replace_if_last_attempt_at: datetime | None = None,
    ) -> None:
        _validate_id("provider_id", provider_id)
        _validate_id("source_id", source_id)
        if (
            isinstance(freshness_seconds, bool)
            or not isinstance(freshness_seconds, int)
            or freshness_seconds <= 0
        ):
            raise ValueError("freshness_seconds must be a positive integer")
        expected = (
            _timestamp(
                replace_if_last_attempt_at.isoformat(),
                "replace_if_last_attempt_at",
            )
            if replace_if_last_attempt_at is not None
            else None
        )
        with self._write_transaction():
            self._record_source_success_locked(
                provider_id,
                source_id,
                attempted_at,
                freshness_seconds,
                expected=expected,
            )

    def _record_source_success_locked(
        self,
        provider_id: str,
        source_id: str,
        attempted_at: datetime,
        freshness_seconds: int,
        *,
        expected: str | None = None,
    ) -> None:
        attempted = _timestamp(attempted_at.isoformat(), "attempted_at")
        stale_at = _timestamp(
            (
                attempted_at.astimezone(timezone.utc)
                + timedelta(seconds=freshness_seconds)
            ).isoformat(),
            "stale_at",
        )
        self._connection.execute(
            "INSERT INTO source_status(provider_id,source_id,state,last_attempt_at,last_success_at,stale_at,error_code) "
            "VALUES(?,?,?,?,?,?,NULL) "
            "ON CONFLICT(provider_id,source_id) DO UPDATE SET "
            "state=excluded.state,last_attempt_at=excluded.last_attempt_at,"
            "last_success_at=excluded.last_success_at,stale_at=excluded.stale_at,error_code=NULL "
            "WHERE excluded.last_attempt_at>=source_status.last_attempt_at OR "
            "(? IS NOT NULL AND source_status.last_attempt_at=?)",
            (
                provider_id,
                source_id,
                "ok",
                attempted,
                attempted,
                stale_at,
                expected,
                expected,
            ),
        )

    def record_source_failure(
        self,
        provider_id: str,
        source_id: str,
        error_code: str | None,
        attempted_at: datetime,
        *,
        replace_if_last_attempt_at: datetime | None = None,
    ) -> None:
        self.record_source_status(
            provider_id,
            source_id,
            "temporarily_unavailable",
            attempted_at,
            error_code,
            replace_if_last_attempt_at=replace_if_last_attempt_at,
        )

    def record_source_status(
        self,
        provider_id: str,
        source_id: str,
        state: str,
        attempted_at: datetime,
        error_code: str | None = None,
        *,
        replace_if_last_attempt_at: datetime | None = None,
    ) -> None:
        _validate_id("provider_id", provider_id)
        _validate_id("source_id", source_id)
        _validate_id("state", state)
        if error_code is not None:
            _validate_id("error_code", error_code)
        attempted = _timestamp(attempted_at.isoformat(), "attempted_at")
        stale_at = attempted if state == "stale" else None
        expected = (
            _timestamp(
                replace_if_last_attempt_at.isoformat(),
                "replace_if_last_attempt_at",
            )
            if replace_if_last_attempt_at is not None
            else None
        )
        with self._write_transaction():
            self._connection.execute(
                "INSERT INTO source_status(provider_id,source_id,state,last_attempt_at,last_success_at,stale_at,error_code) "
                "VALUES(?,?,?,?,NULL,?,?) "
                "ON CONFLICT(provider_id,source_id) DO UPDATE SET "
                "state=excluded.state,last_attempt_at=excluded.last_attempt_at,"
                "stale_at=CASE "
                "WHEN excluded.state='stale' THEN CASE "
                "WHEN source_status.stale_at IS NULL OR excluded.stale_at<source_status.stale_at "
                "THEN excluded.stale_at ELSE source_status.stale_at END "
                "ELSE source_status.stale_at END,"
                "error_code=excluded.error_code "
                "WHERE excluded.last_attempt_at>=source_status.last_attempt_at OR "
                "(? IS NOT NULL AND source_status.last_attempt_at=?)",
                (
                    provider_id,
                    source_id,
                    state,
                    attempted,
                    stale_at,
                    error_code,
                    expected,
                    expected,
                ),
            )

    def source_statuses(self) -> list[SourceStatus]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM source_status ORDER BY provider_id,source_id"
            ).fetchall()
            return [SourceStatus(**dict(row)) for row in rows]

    def delete_source_status(
        self, provider_id: str, source_id: str, attempted_at: datetime
    ) -> None:
        _validate_id("provider_id", provider_id)
        _validate_id("source_id", source_id)
        attempted = _timestamp(attempted_at.isoformat(), "attempted_at")
        with self._write_transaction():
            self._connection.execute(
                "DELETE FROM source_status "
                "WHERE provider_id=? AND source_id=? AND last_attempt_at<=?",
                (provider_id, source_id, attempted),
            )

    def snapshot_source_statuses(self) -> SourceStatusSnapshot:
        with self._read_snapshot():
            rows = self._connection.execute(
                "SELECT * FROM source_status ORDER BY provider_id,source_id"
            ).fetchall()
            return SourceStatusSnapshot(
                tuple(SourceStatus(**dict(row)) for row in rows),
                self._current_change_seq_locked(),
            )

    def apply_retention(
        self, days: int, now: datetime | None = None
    ) -> PurgeResult:
        if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
            raise ValueError("days must be a positive integer")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must include a timezone")
        local_day_cutoff = current.astimezone().date() - timedelta(days=days)
        snapshot_cutoff = current.astimezone(timezone.utc) - timedelta(days=days)
        return self.purge_before(
            local_day_cutoff.isoformat(), snapshot_cutoff.isoformat()
        )

    def _current_change_seq_locked(self) -> int:
        return int(self._connection.execute("SELECT COALESCE(MAX(change_seq),0) FROM change_log").fetchone()[0])

    def changes(self, after: int, limit: int = 100) -> list[ChangeRecord]:
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after must be a nonnegative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM change_log WHERE change_seq>? ORDER BY change_seq LIMIT ?",
                (after, limit),
            ).fetchall()
            return [ChangeRecord(**dict(row)) for row in rows]

    def snapshot_changes(self, after: int, limit: int = 100) -> ChangeSnapshot:
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after must be a nonnegative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._read_snapshot():
            cursor = self._current_change_seq_locked()
            if after > cursor:
                raise ValueError("after cursor is ahead of the ledger high-water mark")
            rows = self._connection.execute(
                "SELECT * FROM change_log WHERE change_seq>? AND change_seq<=? "
                "ORDER BY change_seq LIMIT ?",
                (after, cursor, limit),
            ).fetchall()
            return ChangeSnapshot(
                tuple(ChangeRecord(**dict(row)) for row in rows), cursor
            )

    def purge_before(self, day_cutoff: str, snapshot_cutoff: str) -> PurgeResult:
        _validate_day(day_cutoff)
        normalized_snapshot_cutoff = _timestamp(snapshot_cutoff, "snapshot_cutoff")
        changed_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        with self._write_transaction():
            costs = self._connection.execute(
                "SELECT day,provider_id,account_ref,cost_kind,currency,payload_hash "
                "FROM daily_costs WHERE day<?",
                (day_cutoff,),
            ).fetchall()
            for row in costs:
                record_id = self._cost_record_id(
                    row["day"], row["provider_id"], row["account_ref"],
                    row["cost_kind"], row["currency"],
                )
                self._append_change(
                    "daily_cost", record_id,
                    self._next_revision_locked("daily_cost", record_id),
                    "delete", changed_at, None, str(row["payload_hash"]),
                )
            cost_coverages = self._connection.execute(
                "SELECT day,provider_id,account_ref FROM daily_cost_coverage WHERE day<?",
                (day_cutoff,),
            ).fetchall()
            for row in cost_coverages:
                record_id = self._cost_coverage_record_id(
                    row["day"], row["provider_id"], row["account_ref"]
                )
                self._append_change(
                    "daily_cost_coverage", record_id,
                    self._next_revision_locked("daily_cost_coverage", record_id),
                    "delete", changed_at, None, _hash("null"),
                )
            daily = self._connection.execute(
                "SELECT day,provider_id,account_ref,model_id,revision,payload_hash FROM daily_model_usage WHERE day<?",
                (day_cutoff,),
            ).fetchall()
            for row in daily:
                record_id = self._daily_record_id(
                    row["day"], row["provider_id"], row["account_ref"], row["model_id"]
                )
                self._append_change(
                    "daily_usage",
                    record_id,
                    self._next_revision_locked("daily_usage", record_id),
                    "delete", changed_at, None, str(row["payload_hash"]),
                )
            coverages = self._connection.execute(
                "SELECT day,provider_id,account_ref FROM daily_coverage WHERE day<?", (day_cutoff,)
            ).fetchall()
            for row in coverages:
                record_id = self._coverage_record_id(
                    row["day"], row["provider_id"], row["account_ref"]
                )
                self._append_change(
                    "daily_coverage", record_id,
                    self._next_revision_locked("daily_coverage", record_id),
                    "delete", changed_at, None, _hash("null"),
                )
            self._connection.execute("DELETE FROM daily_model_usage WHERE day<?", (day_cutoff,))
            self._connection.execute("DELETE FROM daily_coverage WHERE day<?", (day_cutoff,))
            self._connection.execute("DELETE FROM daily_costs WHERE day<?", (day_cutoff,))
            self._connection.execute("DELETE FROM daily_cost_coverage WHERE day<?", (day_cutoff,))
            snapshot_count = int(self._connection.execute(
                "SELECT COUNT(*) FROM quota_snapshots WHERE observed_at<?", (normalized_snapshot_cutoff,)
            ).fetchone()[0])
            self._connection.execute("DELETE FROM quota_snapshots WHERE observed_at<?", (normalized_snapshot_cutoff,))
            return PurgeResult(
                len(daily), len(coverages), snapshot_count,
                len(costs), len(cost_coverages),
            )

    def summary(self, start_day: str, end_day: str) -> UsageSummary:
        _validate_day(start_day)
        _validate_day(end_day)
        with self._lock:
            self._connection.execute("BEGIN DEFERRED")
            try:
                total, count = self._connection.execute(
                    "SELECT COALESCE(SUM(total_tokens),0),COUNT(*) FROM daily_model_usage WHERE day BETWEEN ? AND ?",
                    (start_day, end_day),
                ).fetchone()
                covered = self._connection.execute(
                    "SELECT COUNT(*) FROM daily_coverage WHERE day BETWEEN ? AND ?", (start_day, end_day)
                ).fetchone()[0]
                result = UsageSummary(
                    int(total), int(count), int(covered), self._current_change_seq_locked()
                )
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
                return result
