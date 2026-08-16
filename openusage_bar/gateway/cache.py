"""PII-safe, bounded cache storage for the optional Gateway runtime.

The cache accepts only values produced by the sealed PII and streaming
boundaries.  It never accepts caller assertions such as ``redacted=True`` or
``complete=True``.  Exact entries contain only a redacted response.  Prefix
entries are hints and contain no response or prompt material.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import stat
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterator, Mapping

from ..windows_file_security import native_windows_file_security


DEFAULT_CACHE_SIZE_CAP_BYTES = 100 * 1024 * 1024
DEFAULT_CACHE_TTL_SECONDS = 60 * 60

_BUSY_TIMEOUT_SECONDS = 5.0
_BUSY_TIMEOUT_MS = int(_BUSY_TIMEOUT_SECONDS * 1_000)
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_MAX_PARAMETER_BYTES = 64 * 1024
_MAX_KEY_MATERIAL_BYTES = 4 * 1024 * 1024
_MAX_PARAMETER_DEPTH = 16
_MAX_PARAMETER_ITEMS = 1_024
_MAX_PARAMETER_STRING_BYTES = 16 * 1024
_MAX_PREFIXES = 64
_HOT_EXACT_MAX_ENTRIES = 128
_HOT_EXACT_MAX_BYTES = 8 * 1024 * 1024
_PENDING_TOUCH_MAX_ENTRIES = 256
_DATABASE_FILENAME = "gateway-cache.sqlite3"
_KEY_DOMAIN = b"openusage-gateway-cache-key/v1\0"
_STABLE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_HEX_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_RESERVED_PLACEHOLDER = re.compile(
    r"__OPENUSAGE_[A-Z][A-Z0-9]*_[1-9][0-9]*__"
)
_PROVIDERS = frozenset(
    {"anthropic", "deepseek", "ollama", "openai", "openrouter"}
)
_SENSITIVE_PARAMETER_NAMES = frozenset(
    {
        "api_key",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "id_token",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "token",
    }
)
_EMPTY_METADATA_JSON = "{}"
_EPHEMERAL_METADATA_JSON = '{"cache_control":"ephemeral"}'
_ALLOWED_METADATA_JSON = frozenset(
    {_EMPTY_METADATA_JSON, _EPHEMERAL_METADATA_JSON}
)

_CONFIG_ERROR = "invalid cache configuration"
_PATH_ERROR = "invalid cache database path"
_ENTRY_ERROR = "invalid cache entry"
_CLOCK_ERROR = "invalid cache clock"
_CLOSED_ERROR = "cache is closed"
_SCHEMA_ERROR = "cache database schema is incompatible"
_INITIALIZATION_ERROR = "cache database initialization failed"
_OPERATION_ERROR = "cache database operation failed"

_EXACT_TABLE = "exact_entries"
_PREFIX_TABLE = "prefix_entries"
_EXACT_ACCESSED_INDEX = "exact_entries_accessed_at_idx"
_EXACT_EXPIRES_INDEX = "exact_entries_expires_at_idx"
_PREFIX_EXPIRES_INDEX = "prefix_entries_expires_at_idx"

_EXACT_TABLE_SIGNATURE = (
    (0, "key_hash", "TEXT", 1, None, 1, 0),
    (1, "redacted_response", "TEXT", 1, None, 0, 0),
    (2, "response_sha256", "TEXT", 1, None, 0, 0),
    (3, "size_bytes", "INTEGER", 1, None, 0, 0),
    (4, "created_at", "TEXT", 1, None, 0, 0),
    (5, "accessed_at", "TEXT", 1, None, 0, 0),
    (6, "expires_at", "TEXT", 1, None, 0, 0),
    (7, "hit_count", "INTEGER", 1, "0", 0, 0),
)
_PREFIX_TABLE_SIGNATURE = (
    (0, "prefix_hash", "TEXT", 1, None, 1, 0),
    (1, "source_key_hash", "TEXT", 1, None, 2, 0),
    (2, "depth", "INTEGER", 1, None, 0, 0),
    (3, "provider_metadata", "TEXT", 1, None, 0, 0),
    (4, "size_bytes", "INTEGER", 1, None, 0, 0),
    (5, "created_at", "TEXT", 1, None, 0, 0),
    (6, "accessed_at", "TEXT", 1, None, 0, 0),
    (7, "expires_at", "TEXT", 1, None, 0, 0),
    (8, "hit_count", "INTEGER", 1, "0", 0, 0),
)
_EXPECTED_OBJECTS = {
    ("table", _EXACT_TABLE),
    ("table", _PREFIX_TABLE),
    ("index", _EXACT_ACCESSED_INDEX),
    ("index", _EXACT_EXPIRES_INDEX),
    ("index", _PREFIX_EXPIRES_INDEX),
}

# These signatures are process-local admission seals, not credentials.  They
# prevent a caller from fabricating a cache candidate by copying public fields
# or toggling booleans.
_ADMISSION_KEY = os.urandom(32)
_WINDOWS_FILE_SECURITY = native_windows_file_security()


@dataclass(frozen=True)
class GatewayCacheStats:
    entries: int
    size_bytes: int


@dataclass(frozen=True)
class CacheKeySet:
    """Opaque, provenance-bound L1 and L2 cache keys."""

    exact_sha256: str
    prefix_sha256: tuple[str, ...]
    _request_source_sha256: str = field(repr=False)
    _request_lineage: object = field(repr=False, compare=False)
    _request_placeholders: tuple[str, ...] = field(repr=False)
    _request_privacy_discriminator: str | None = field(repr=False)
    _prefix_source_sha256: tuple[str, ...] = field(repr=False)
    _prefix_lineage: tuple[object, ...] = field(repr=False, compare=False)
    _prefix_privacy_discriminator: tuple[str | None, ...] = field(repr=False)
    _cacheable: bool = field(repr=False)
    _signature: bytes = field(repr=False, compare=False)

    @classmethod
    def from_request(
        cls,
        *,
        provider_id: str,
        model: str,
        parameters: dict[str, object],
        request: object,
        prefixes: tuple[object, ...],
        stream: bool,
    ) -> CacheKeySet:
        request_provenance = _redaction_provenance(request)
        if (
            request_provenance is None
            or request_provenance.role != "request"
            or type(stream) is not bool
            or type(prefixes) is not tuple
            or len(prefixes) > _MAX_PREFIXES
        ):
            raise ValueError(_ENTRY_ERROR)

        provider = _validated_provider(provider_id)
        model_id = _validated_model(model)
        normalized_parameters = _canonical_parameters(parameters)
        request_text = _verified_redacted_text(request, request_provenance)

        prefix_material: list[tuple[str, _RedactionProvenance]] = []
        for prefix in prefixes:
            provenance = _redaction_provenance(prefix)
            if provenance is None or provenance.role != "request":
                raise ValueError(_ENTRY_ERROR)
            prefix_material.append(
                (_verified_redacted_text(prefix, provenance), provenance)
            )

        common = {
            "model": model_id,
            "parameters": normalized_parameters,
            "provider": provider,
            "stream": stream,
        }
        exact_material = {**common, "kind": "exact", "request": request_text}
        if request_provenance.privacy_discriminator is not None:
            exact_material["privacy_context"] = (
                request_provenance.privacy_discriminator
            )
        exact_hash = _cache_hash(exact_material)

        prefix_hashes_list: list[str] = []
        for depth, (prefix_text, provenance) in enumerate(
            prefix_material, start=1
        ):
            prefix_key_material = {
                **common,
                "depth": depth,
                "kind": "prefix",
                "request_prefix": prefix_text,
            }
            if request_provenance.privacy_discriminator is not None:
                prefix_key_material["privacy_context"] = (
                    request_provenance.privacy_discriminator
                )
            if provenance.privacy_discriminator is not None:
                prefix_key_material["prefix_privacy_context"] = (
                    provenance.privacy_discriminator
                )
            prefix_hashes_list.append(_cache_hash(prefix_key_material))
        prefix_hashes = tuple(prefix_hashes_list)
        cacheable = request_provenance.cacheable and all(
            provenance.cacheable for _, provenance in prefix_material
        )
        placeholders = tuple(sorted(request_provenance.placeholders))
        prefix_sources = tuple(
            provenance.source_sha256 for _, provenance in prefix_material
        )
        prefix_lineages = tuple(
            provenance.lineage for _, provenance in prefix_material
        )
        prefix_privacy_discriminators = tuple(
            provenance.privacy_discriminator
            for _, provenance in prefix_material
        )
        signature = _key_set_signature(
            exact_hash=exact_hash,
            prefix_hashes=prefix_hashes,
            request_source_sha256=request_provenance.source_sha256,
            request_lineage=request_provenance.lineage,
            request_placeholders=placeholders,
            request_privacy_discriminator=(
                request_provenance.privacy_discriminator
            ),
            prefix_source_sha256=prefix_sources,
            prefix_lineage=prefix_lineages,
            prefix_privacy_discriminator=prefix_privacy_discriminators,
            cacheable=cacheable,
        )
        return cls(
            exact_sha256=exact_hash,
            prefix_sha256=prefix_hashes,
            _request_source_sha256=request_provenance.source_sha256,
            _request_lineage=request_provenance.lineage,
            _request_placeholders=placeholders,
            _request_privacy_discriminator=(
                request_provenance.privacy_discriminator
            ),
            _prefix_source_sha256=prefix_sources,
            _prefix_lineage=prefix_lineages,
            _prefix_privacy_discriminator=prefix_privacy_discriminators,
            _cacheable=cacheable,
            _signature=signature,
        )


@dataclass(frozen=True)
class CacheCandidate:
    """A sealed, cache-eligible redacted response and its prefix hints."""

    _exact_sha256: str = field(repr=False)
    _prefix_sha256: tuple[str, ...] = field(repr=False)
    _redacted_response: str = field(repr=False)
    _response_sha256: str = field(repr=False)
    _provider_metadata_json: str = field(repr=False)
    _request_source_sha256: str = field(repr=False)
    _response_source_sha256: str = field(repr=False)
    _completion_body_sha256: str = field(repr=False)
    _privacy_discriminator: str | None = field(repr=False)
    _signature: bytes = field(repr=False, compare=False)

    @classmethod
    def create(
        cls,
        *,
        keys: CacheKeySet,
        request: object,
        response: object,
        completion: object,
        provider_metadata: dict[str, str],
    ) -> CacheCandidate | None:
        if not _valid_key_set(keys):
            return None
        request_provenance = _redaction_provenance(request)
        response_provenance = _redaction_provenance(response)
        completion_provenance = _stream_provenance(completion)
        if (
            request_provenance is None
            or response_provenance is None
            or completion_provenance is None
            or request_provenance.role != "request"
            or response_provenance.role != "response"
            or not keys._cacheable
            or not request_provenance.cacheable
            or not response_provenance.cacheable
            or completion_provenance.status != "complete"
            or not completion_provenance.cacheable
            or completion_provenance.has_tool_use
        ):
            return None
        if (
            request_provenance.source_sha256
            != keys._request_source_sha256
            or request_provenance.lineage is not keys._request_lineage
            or tuple(sorted(request_provenance.placeholders))
            != keys._request_placeholders
            or request_provenance.privacy_discriminator
            != keys._request_privacy_discriminator
            or response_provenance.lineage is not request_provenance.lineage
            or response_provenance.privacy_discriminator
            != request_provenance.privacy_discriminator
            or not response_provenance.mapping_placeholders.issubset(
                request_provenance.mapping_placeholders
            )
            or not response_provenance.used_placeholders.issubset(
                request_provenance.mapping_placeholders
            )
        ):
            return None

        redacted_response = _verified_redacted_text(
            response, response_provenance
        )
        if response_provenance.source_sha256 != completion_provenance.text_sha256:
            return None
        metadata_json = _canonical_provider_metadata(provider_metadata)
        if metadata_json is None:
            return None

        response_sha256 = _sha256_text(redacted_response)
        signature = _candidate_signature(
            exact_hash=keys.exact_sha256,
            prefix_hashes=keys.prefix_sha256,
            redacted_response=redacted_response,
            response_sha256=response_sha256,
            provider_metadata_json=metadata_json,
            request_source_sha256=request_provenance.source_sha256,
            response_source_sha256=response_provenance.source_sha256,
            completion_body_sha256=completion_provenance.canonical_body_sha256,
            privacy_discriminator=request_provenance.privacy_discriminator,
        )
        return cls(
            _exact_sha256=keys.exact_sha256,
            _prefix_sha256=keys.prefix_sha256,
            _redacted_response=redacted_response,
            _response_sha256=response_sha256,
            _provider_metadata_json=metadata_json,
            _request_source_sha256=request_provenance.source_sha256,
            _response_source_sha256=response_provenance.source_sha256,
            _completion_body_sha256=completion_provenance.canonical_body_sha256,
            _privacy_discriminator=request_provenance.privacy_discriminator,
            _signature=signature,
        )


@dataclass(frozen=True)
class ExactHit:
    exact_sha256: str
    redacted_response: str = field(repr=False)


@dataclass(frozen=True)
class PrefixHint:
    prefix_sha256: str
    depth: int
    provider_metadata: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True)
class Miss:
    """Cache miss marker with no request or response material."""


@dataclass(frozen=True)
class _RedactionProvenance:
    role: str
    lineage: object
    source_sha256: str
    redacted_sha256: str
    mapping_placeholders: frozenset[str]
    used_placeholders: frozenset[str]
    cacheable: bool
    privacy_discriminator: str | None = field(repr=False)

    @property
    def placeholders(self) -> frozenset[str]:
        return self.mapping_placeholders


@dataclass(frozen=True)
class _StreamProvenance:
    status: str
    cacheable: bool
    has_tool_use: bool
    text_sha256: str
    canonical_body_sha256: str


@dataclass(frozen=True)
class _DatabaseIdentity:
    parent_device: int
    parent_inode: int
    database_device: int
    database_inode: int


@dataclass(frozen=True)
class _HotExactEntry:
    redacted_response: str = field(repr=False)
    expires_at: datetime
    size_bytes: int


class SQLiteGatewayCache:
    """SQLite-backed exact cache and response-free prefix-hint index."""

    def __init__(
        self,
        path: str | Path,
        *,
        enabled: bool = True,
        size_cap_bytes: int = DEFAULT_CACHE_SIZE_CAP_BYTES,
        ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            type(enabled) is not bool
            or not _positive_int(size_cap_bytes)
            or not _positive_int(ttl_seconds)
            or (clock is not None and not callable(clock))
        ):
            raise ValueError(_CONFIG_ERROR)
        self.path = _coerce_database_path(path)
        self.enabled = enabled
        self.size_cap_bytes = size_cap_bytes
        self.ttl_seconds = ttl_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._closed = False
        self._identity: _DatabaseIdentity | None = None
        self._connection: sqlite3.Connection | None = None
        self._hot_exact: OrderedDict[str, _HotExactEntry] = OrderedDict()
        self._hot_exact_size_bytes = 0
        self._pending_exact_touches: OrderedDict[
            str, tuple[datetime, int]
        ] = OrderedDict()
        self.journal_mode = "disabled"

        if not enabled:
            return
        try:
            self._identity = _prepare_private_database(self.path)
            self.journal_mode = self._initialize_database()
            self._connection = self._open_configured_connection()
        except ValueError:
            raise
        except (OSError, sqlite3.Error):
            raise RuntimeError(_INITIALIZATION_ERROR) from None

    def close(self) -> None:
        """Close the hardened connection; safe to call repeatedly."""

        with self._lock:
            if self._closed:
                return
            connection = self._connection
            try:
                if connection is not None and self._pending_exact_touches:
                    with self._write_connection() as writable:
                        self._flush_pending_exact_touches(writable)
            finally:
                self._closed = True
                self._connection = None
                self._clear_hot_exact()
                self._pending_exact_touches.clear()
                if connection is not None:
                    connection.close()
                    self._tighten_database_files()

    def store(self, candidate: CacheCandidate) -> bool:
        """Persist a sealed candidate; caller-provided safety flags are absent."""

        if not _valid_candidate(candidate):
            raise ValueError(_ENTRY_ERROR)
        with self._lock:
            self._require_open()
            if not self.enabled:
                return False
            now = self._now()
            expires_at = now + timedelta(seconds=self.ttl_seconds)
            exact_size = _exact_entry_size(
                candidate._exact_sha256,
                candidate._redacted_response,
                candidate._response_sha256,
            )
            prefix_sizes = tuple(
                _prefix_entry_size(
                    prefix_hash,
                    candidate._exact_sha256,
                    depth,
                    candidate._provider_metadata_json,
                )
                for depth, prefix_hash in enumerate(
                    candidate._prefix_sha256, start=1
                )
            )
            candidate_size = exact_size + sum(prefix_sizes)
            self._clear_hot_exact()
            try:
                self._pending_exact_touches.pop(candidate._exact_sha256, None)
                with self._write_connection() as connection:
                    self._flush_pending_exact_touches(connection)
                    self._delete_expired(connection, now)
                    if candidate_size > self.size_cap_bytes:
                        connection.execute(
                            "DELETE FROM exact_entries WHERE key_hash = ?",
                            (candidate._exact_sha256,),
                        )
                        return False
                    connection.execute(
                        "DELETE FROM exact_entries WHERE key_hash = ?",
                        (candidate._exact_sha256,),
                    )
                    timestamp = _iso(now)
                    expiry = _iso(expires_at)
                    connection.execute(
                        """
                        INSERT INTO exact_entries (
                            key_hash, redacted_response, response_sha256,
                            size_bytes, created_at, accessed_at, expires_at,
                            hit_count
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                        """,
                        (
                            candidate._exact_sha256,
                            candidate._redacted_response,
                            candidate._response_sha256,
                            exact_size,
                            timestamp,
                            timestamp,
                            expiry,
                        ),
                    )
                    for depth, (prefix_hash, size_bytes) in enumerate(
                        zip(candidate._prefix_sha256, prefix_sizes), start=1
                    ):
                        connection.execute(
                            """
                            INSERT INTO prefix_entries (
                                prefix_hash, source_key_hash, depth,
                                provider_metadata, size_bytes, created_at,
                                accessed_at, expires_at, hit_count
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                            """,
                            (
                                prefix_hash,
                                candidate._exact_sha256,
                                depth,
                                candidate._provider_metadata_json,
                                size_bytes,
                                timestamp,
                                timestamp,
                                expiry,
                            ),
                        )
                    self._enforce_size_cap(connection)
                    retained = connection.execute(
                        "SELECT 1 FROM exact_entries WHERE key_hash = ?",
                        (candidate._exact_sha256,),
                    ).fetchone()
            except ValueError:
                raise
            except (OSError, sqlite3.Error):
                raise RuntimeError(_OPERATION_ERROR) from None
        return retained is not None

    def lookup(self, keys: CacheKeySet) -> ExactHit | PrefixHint | Miss:
        """Return a redacted exact response or a response-free prefix hint."""

        if not _valid_key_set(keys):
            raise ValueError(_ENTRY_ERROR)
        with self._lock:
            self._require_open()
            if not self.enabled or not keys._cacheable:
                return Miss()
            now = self._now()
            try:
                hot = self._lookup_hot_exact(keys, now)
                if hot is not None:
                    return hot
                with self._write_connection() as connection:
                    self._flush_pending_exact_touches(connection)
                    self._delete_expired(connection, now)
                    exact = self._lookup_exact(connection, keys, now)
                    if exact is not None:
                        return exact
                    hint = self._lookup_prefix(connection, keys, now)
                    return hint if hint is not None else Miss()
            except ValueError:
                raise
            except (OSError, sqlite3.Error):
                raise RuntimeError(_OPERATION_ERROR) from None

    def clear(self) -> None:
        with self._lock:
            self._require_open()
            if not self.enabled:
                return
            try:
                self._clear_hot_exact()
                self._pending_exact_touches.clear()
                with self._write_connection() as connection:
                    connection.execute("DELETE FROM exact_entries")
                    connection.execute("DELETE FROM prefix_entries")
            except ValueError:
                raise
            except (OSError, sqlite3.Error):
                raise RuntimeError(_OPERATION_ERROR) from None

    def stats(self) -> GatewayCacheStats:
        with self._lock:
            self._require_open()
            if not self.enabled:
                return GatewayCacheStats(entries=0, size_bytes=0)
            now = self._now()
            try:
                with self._write_connection() as connection:
                    self._flush_pending_exact_touches(connection)
                    self._delete_expired(connection, now)
                    entries, exact_size = connection.execute(
                        "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) "
                        "FROM exact_entries"
                    ).fetchone()
                    prefix_size = connection.execute(
                        "SELECT COALESCE(SUM(size_bytes), 0) FROM prefix_entries"
                    ).fetchone()[0]
            except ValueError:
                raise
            except (OSError, sqlite3.Error):
                raise RuntimeError(_OPERATION_ERROR) from None
        return GatewayCacheStats(
            entries=int(entries),
            size_bytes=int(exact_size) + int(prefix_size),
        )

    def _lookup_exact(
        self,
        connection: sqlite3.Connection,
        keys: CacheKeySet,
        now: datetime,
    ) -> ExactHit | None:
        row = connection.execute(
            """
            SELECT redacted_response, response_sha256, size_bytes, expires_at
            FROM exact_entries WHERE key_hash = ?
            """,
            (keys.exact_sha256,),
        ).fetchone()
        if row is None:
            return None
        response, response_sha256, size_bytes, expires_at_raw = row
        expires_at = _stored_datetime(expires_at_raw)
        valid = (
            type(response) is str
            and _valid_sha256(response_sha256)
            and _sha256_text(response) == response_sha256
            and _safe_stored_response(response, keys)
            and type(size_bytes) is int
            and size_bytes
            == _exact_entry_size(keys.exact_sha256, response, response_sha256)
            and expires_at is not None
            and now < expires_at
        )
        if not valid:
            connection.execute(
                "DELETE FROM exact_entries WHERE key_hash = ?",
                (keys.exact_sha256,),
            )
            return None
        connection.execute(
            """
            UPDATE exact_entries
            SET accessed_at = ?, hit_count = hit_count + 1
            WHERE key_hash = ?
            """,
            (_iso(now), keys.exact_sha256),
        )
        self._remember_hot_exact(
            keys.exact_sha256,
            response,
            expires_at,
            size_bytes,
        )
        return ExactHit(
            exact_sha256=keys.exact_sha256,
            redacted_response=response,
        )

    def _lookup_prefix(
        self,
        connection: sqlite3.Connection,
        keys: CacheKeySet,
        now: datetime,
    ) -> PrefixHint | None:
        timestamp = _iso(now)
        for depth in range(len(keys.prefix_sha256), 0, -1):
            prefix_hash = keys.prefix_sha256[depth - 1]
            rows = connection.execute(
                """
                SELECT source_key_hash, provider_metadata, size_bytes
                FROM prefix_entries
                WHERE prefix_hash = ? AND depth = ?
                ORDER BY accessed_at DESC, created_at DESC, source_key_hash ASC
                """,
                (prefix_hash, depth),
            ).fetchall()
            for source_key_hash, metadata_json, size_bytes in rows:
                valid = (
                    _valid_sha256(source_key_hash)
                    and metadata_json in _ALLOWED_METADATA_JSON
                    and type(size_bytes) is int
                    and size_bytes
                    == _prefix_entry_size(
                        prefix_hash,
                        source_key_hash,
                        depth,
                        metadata_json,
                    )
                )
                if not valid:
                    connection.execute(
                        """
                        DELETE FROM prefix_entries
                        WHERE prefix_hash = ? AND source_key_hash = ?
                        """,
                        (prefix_hash, source_key_hash),
                    )
                    continue
                connection.execute(
                    """
                    UPDATE prefix_entries
                    SET accessed_at = ?, hit_count = hit_count + 1
                    WHERE prefix_hash = ? AND source_key_hash = ?
                    """,
                    (timestamp, prefix_hash, source_key_hash),
                )
                connection.execute(
                    "UPDATE exact_entries SET accessed_at = ? WHERE key_hash = ?",
                    (timestamp, source_key_hash),
                )
                metadata = json.loads(metadata_json)
                return PrefixHint(
                    prefix_sha256=prefix_hash,
                    depth=depth,
                    provider_metadata=MappingProxyType(metadata),
                )
        return None

    def _lookup_hot_exact(
        self,
        keys: CacheKeySet,
        now: datetime,
    ) -> ExactHit | None:
        entry = self._hot_exact.get(keys.exact_sha256)
        if entry is None:
            return None
        if now >= entry.expires_at:
            self._forget_hot_exact(keys.exact_sha256)
            self._pending_exact_touches.pop(keys.exact_sha256, None)
            return None

        # The response was fully revalidated before entering L1.  Keep the
        # filesystem identity and private-mode checks on every replay so the
        # fast path cannot turn a path swap or permission drift into a bypass.
        self._verify_hot_database_files()
        self._hot_exact.move_to_end(keys.exact_sha256)
        self._record_pending_exact_touch(keys.exact_sha256, now)
        return ExactHit(
            exact_sha256=keys.exact_sha256,
            redacted_response=entry.redacted_response,
        )

    def _remember_hot_exact(
        self,
        key_hash: str,
        redacted_response: str,
        expires_at: datetime,
        size_bytes: int,
    ) -> None:
        self._forget_hot_exact(key_hash)
        if size_bytes > _HOT_EXACT_MAX_BYTES:
            return
        self._hot_exact[key_hash] = _HotExactEntry(
            redacted_response=redacted_response,
            expires_at=expires_at,
            size_bytes=size_bytes,
        )
        self._hot_exact_size_bytes += size_bytes
        while (
            len(self._hot_exact) > _HOT_EXACT_MAX_ENTRIES
            or self._hot_exact_size_bytes > _HOT_EXACT_MAX_BYTES
        ):
            _, removed = self._hot_exact.popitem(last=False)
            self._hot_exact_size_bytes -= removed.size_bytes

    def _forget_hot_exact(self, key_hash: str) -> None:
        existing = self._hot_exact.pop(key_hash, None)
        if existing is not None:
            self._hot_exact_size_bytes -= existing.size_bytes

    def _clear_hot_exact(self) -> None:
        self._hot_exact.clear()
        self._hot_exact_size_bytes = 0

    def _record_pending_exact_touch(
        self,
        key_hash: str,
        now: datetime,
    ) -> None:
        existing = self._pending_exact_touches.get(key_hash)
        if (
            existing is None
            and len(self._pending_exact_touches) >= _PENDING_TOUCH_MAX_ENTRIES
        ):
            with self._write_connection() as connection:
                self._flush_pending_exact_touches(connection)
        count = 1 if existing is None else min(_MAX_SQLITE_INTEGER, existing[1] + 1)
        self._pending_exact_touches[key_hash] = (now, count)
        self._pending_exact_touches.move_to_end(key_hash)

    def _flush_pending_exact_touches(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        pending = tuple(self._pending_exact_touches.items())
        if not pending:
            return
        for key_hash, (accessed_at, count) in pending:
            connection.execute(
                """
                UPDATE exact_entries
                SET accessed_at = ?,
                    hit_count = MIN(hit_count + ?, ?)
                WHERE key_hash = ?
                """,
                (_iso(accessed_at), count, _MAX_SQLITE_INTEGER, key_hash),
            )
        self._pending_exact_touches.clear()

    def _initialize_database(self) -> str:
        with self._configured_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                objects = _database_objects(connection)
                if objects:
                    _validate_schema(connection)
                else:
                    _create_schema(connection)
                    _validate_schema(connection)
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

            journal_mode = str(
                connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            ).casefold()
            if journal_mode != "wal":
                raise sqlite3.OperationalError("WAL unavailable")
            with self._transaction(connection):
                self._delete_expired(connection, self._now())
        self._tighten_database_files()
        return journal_mode

    @contextmanager
    def _configured_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_configured_connection()
        try:
            yield connection
        finally:
            connection.close()
            self._tighten_database_files()

    def _open_configured_connection(self) -> sqlite3.Connection:
        identity = self._require_identity()
        _verify_database_identity(self.path, identity)
        _verify_sidecars(self.path)
        connection = sqlite3.connect(
            self.path,
            timeout=_BUSY_TIMEOUT_SECONDS,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            _verify_database_identity(self.path, identity)
            connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA foreign_keys=ON")
            if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
                raise sqlite3.OperationalError("foreign keys unavailable")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA secure_delete=ON")
            return connection
        except Exception:
            connection.close()
            self._tighten_database_files()
            raise

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._require_connection()
        identity = self._require_identity()
        _verify_database_identity(self.path, identity)
        _verify_sidecars(self.path)
        try:
            with self._transaction(connection):
                yield connection
        finally:
            self._tighten_database_files()

    @staticmethod
    @contextmanager
    def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()

    @staticmethod
    def _delete_expired(
        connection: sqlite3.Connection, now: datetime
    ) -> None:
        timestamp = _iso(now)
        connection.execute(
            "DELETE FROM exact_entries WHERE expires_at <= ?",
            (timestamp,),
        )
        connection.execute(
            "DELETE FROM prefix_entries WHERE expires_at <= ?",
            (timestamp,),
        )

    def _enforce_size_cap(self, connection: sqlite3.Connection) -> None:
        while _stored_size(connection) > self.size_cap_bytes:
            row = connection.execute(
                """
                SELECT key_hash FROM exact_entries
                ORDER BY accessed_at ASC, created_at ASC, key_hash ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                raise sqlite3.DatabaseError("cache size invariant failed")
            connection.execute(
                "DELETE FROM exact_entries WHERE key_hash = ?", (row[0],)
            )

    def _now(self) -> datetime:
        try:
            value = self._clock()
        except Exception:
            raise ValueError(_CLOCK_ERROR) from None
        try:
            if (
                not isinstance(value, datetime)
                or value.tzinfo is None
                or value.utcoffset() is None
            ):
                raise ValueError(_CLOCK_ERROR)
            return value.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(_CLOCK_ERROR) from None

    def _tighten_database_files(self) -> None:
        identity = self._require_identity()
        if _WINDOWS_FILE_SECURITY is not None:
            _WINDOWS_FILE_SECURITY.harden_directory(self.path.parent)
        _tighten_private_file(
            self.path,
            expected=(identity.database_device, identity.database_inode),
            required=True,
        )
        for sidecar in (Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            _tighten_private_file(sidecar, expected=None, required=False)
        _verify_database_identity(self.path, identity)
        _verify_sidecars(self.path)

    def _verify_hot_database_files(self) -> None:
        if os.name == "nt":
            self._tighten_database_files()
            return
        identity = self._require_identity()
        _verify_database_identity(self.path, identity)
        _verify_private_file(
            self.path,
            expected=(identity.database_device, identity.database_inode),
            required=True,
        )
        for sidecar in (Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            _verify_private_file(sidecar, expected=None, required=False)

    def _require_identity(self) -> _DatabaseIdentity:
        if self._identity is None:
            raise ValueError(_PATH_ERROR)
        return self._identity

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError(_CLOSED_ERROR)

    def _require_connection(self) -> sqlite3.Connection:
        self._require_open()
        if self._connection is None:
            raise RuntimeError(_CLOSED_ERROR)
        return self._connection


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE exact_entries (
            key_hash TEXT PRIMARY KEY NOT NULL
                CHECK(length(key_hash) = 64
                    AND key_hash NOT GLOB '*[^0-9a-f]*'),
            redacted_response TEXT NOT NULL,
            response_sha256 TEXT NOT NULL
                CHECK(length(response_sha256) = 64
                    AND response_sha256 NOT GLOB '*[^0-9a-f]*'),
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            created_at TEXT NOT NULL,
            accessed_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 0 CHECK(hit_count >= 0)
        ) STRICT
        """
    )
    connection.execute(
        """
        CREATE TABLE prefix_entries (
            prefix_hash TEXT NOT NULL
                CHECK(length(prefix_hash) = 64
                    AND prefix_hash NOT GLOB '*[^0-9a-f]*'),
            source_key_hash TEXT NOT NULL,
            depth INTEGER NOT NULL CHECK(depth BETWEEN 1 AND 64),
            provider_metadata TEXT NOT NULL
                CHECK(provider_metadata IN ('{}',
                    '{"cache_control":"ephemeral"}')),
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            created_at TEXT NOT NULL,
            accessed_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 0 CHECK(hit_count >= 0),
            PRIMARY KEY(prefix_hash, source_key_hash),
            FOREIGN KEY(source_key_hash) REFERENCES exact_entries(key_hash)
                ON DELETE CASCADE
        ) STRICT
        """
    )
    connection.execute(
        f"CREATE INDEX {_EXACT_ACCESSED_INDEX} ON exact_entries(accessed_at)"
    )
    connection.execute(
        f"CREATE INDEX {_EXACT_EXPIRES_INDEX} ON exact_entries(expires_at)"
    )
    connection.execute(
        f"CREATE INDEX {_PREFIX_EXPIRES_INDEX} ON prefix_entries(expires_at)"
    )


def _validated_provider(value: object) -> str:
    if type(value) is not str or value not in _PROVIDERS:
        raise ValueError(_ENTRY_ERROR)
    return value


def _validated_model(value: object) -> str:
    if type(value) is not str or _STABLE_ID.fullmatch(value) is None:
        raise ValueError(_ENTRY_ERROR)
    if not _pii_neutral_text(value):
        raise ValueError(_ENTRY_ERROR)
    return value


def _canonical_parameters(value: object) -> object:
    if type(value) is not dict:
        raise ValueError(_ENTRY_ERROR)
    counter = [0]
    normalized = _validated_json_value(value, depth=0, counter=counter)
    try:
        encoded = _canonical_json_bytes(normalized)
    except (TypeError, ValueError, UnicodeError):
        raise ValueError(_ENTRY_ERROR) from None
    if len(encoded) > _MAX_PARAMETER_BYTES:
        raise ValueError(_ENTRY_ERROR)
    return normalized


def _validated_json_value(
    value: object, *, depth: int, counter: list[int]
) -> object:
    if depth > _MAX_PARAMETER_DEPTH:
        raise ValueError(_ENTRY_ERROR)
    counter[0] += 1
    if counter[0] > _MAX_PARAMETER_ITEMS:
        raise ValueError(_ENTRY_ERROR)
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > _MAX_SQLITE_INTEGER:
            raise ValueError(_ENTRY_ERROR)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(_ENTRY_ERROR)
        return value
    if type(value) is str:
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            raise ValueError(_ENTRY_ERROR) from None
        if (
            len(encoded) > _MAX_PARAMETER_STRING_BYTES
            or not _pii_neutral_text(value)
        ):
            raise ValueError(_ENTRY_ERROR)
        return value
    if type(value) is list:
        return [
            _validated_json_value(item, depth=depth + 1, counter=counter)
            for item in value
        ]
    if type(value) is dict:
        normalized: dict[str, object] = {}
        for key, item in value.items():
            try:
                key_size = len(key.encode("utf-8")) if type(key) is str else 0
            except UnicodeError:
                raise ValueError(_ENTRY_ERROR) from None
            if (
                type(key) is not str
                or not key
                or key_size > 256
                or _sensitive_parameter_name(key)
                or not _pii_neutral_text(key)
            ):
                raise ValueError(_ENTRY_ERROR)
            normalized[key] = _validated_json_value(
                item, depth=depth + 1, counter=counter
            )
        return normalized
    raise ValueError(_ENTRY_ERROR)


def _sensitive_parameter_name(value: str) -> bool:
    normalized = value.casefold().replace("-", "_")
    return normalized in _SENSITIVE_PARAMETER_NAMES or normalized.endswith(
        ("_api_key", "_password", "_secret", "_cookie")
    )


def _pii_neutral_text(value: str) -> bool:
    try:
        from .pii import Redactor

        result = Redactor().redact(value)
        provenance = _redaction_provenance(result)
    except (ImportError, TypeError, ValueError):
        return False
    return bool(
        provenance is not None
        and provenance.role == "request"
        and provenance.cacheable
        and not provenance.placeholders
        and result.redacted_text == value
    )


def _safe_stored_response(value: str, keys: CacheKeySet) -> bool:
    placeholders = frozenset(_RESERVED_PLACEHOLDER.findall(value))
    if not placeholders.issubset(keys._request_placeholders):
        return False
    masked = value
    for placeholder in sorted(placeholders, key=len, reverse=True):
        masked = masked.replace(placeholder, "OPENUSAGE_REDACTED")
    return _pii_neutral_text(masked)


def _canonical_provider_metadata(value: object) -> str | None:
    if type(value) is not dict:
        return None
    if not value:
        return _EMPTY_METADATA_JSON
    if value == {"cache_control": "ephemeral"} and all(
        type(key) is str and type(item) is str for key, item in value.items()
    ):
        return _EPHEMERAL_METADATA_JSON
    return None


def _cache_hash(payload: object) -> str:
    try:
        canonical = _canonical_json_bytes(payload)
    except (TypeError, ValueError, UnicodeError):
        raise ValueError(_ENTRY_ERROR) from None
    if not canonical or len(canonical) > _MAX_KEY_MATERIAL_BYTES:
        raise ValueError(_ENTRY_ERROR)
    return hashlib.sha256(_KEY_DOMAIN + canonical).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _key_set_signature(
    *,
    exact_hash: str,
    prefix_hashes: tuple[str, ...],
    request_source_sha256: str,
    request_lineage: object,
    request_placeholders: tuple[str, ...],
    request_privacy_discriminator: str | None,
    prefix_source_sha256: tuple[str, ...],
    prefix_lineage: tuple[object, ...],
    prefix_privacy_discriminator: tuple[str | None, ...],
    cacheable: bool,
) -> bytes:
    return _admission_signature(
        "key-set",
        {
            "cacheable": cacheable,
            "exact": exact_hash,
            "lineage": _lineage_token(request_lineage),
            "prefixes": prefix_hashes,
            "prefix_lineage": tuple(
                _lineage_token(lineage) for lineage in prefix_lineage
            ),
            "prefix_privacy_discriminator": prefix_privacy_discriminator,
            "prefix_sources": prefix_source_sha256,
            "request_placeholders": request_placeholders,
            "request_privacy_discriminator": request_privacy_discriminator,
            "source": request_source_sha256,
        },
    )


def _candidate_signature(
    *,
    exact_hash: str,
    prefix_hashes: tuple[str, ...],
    redacted_response: str,
    response_sha256: str,
    provider_metadata_json: str,
    request_source_sha256: str,
    response_source_sha256: str,
    completion_body_sha256: str,
    privacy_discriminator: str | None,
) -> bytes:
    return _admission_signature(
        "candidate",
        {
            "completion_body": completion_body_sha256,
            "exact": exact_hash,
            "metadata": provider_metadata_json,
            "privacy_discriminator": privacy_discriminator,
            "prefixes": prefix_hashes,
            "redacted_response": redacted_response,
            "request_source": request_source_sha256,
            "response_sha256": response_sha256,
            "response_source": response_source_sha256,
        },
    )


def _admission_signature(label: str, payload: object) -> bytes:
    message = label.encode("ascii") + b"\0" + _canonical_json_bytes(payload)
    return hmac.digest(_ADMISSION_KEY, message, "sha256")


def _valid_key_set(value: object) -> bool:
    if type(value) is not CacheKeySet:
        return False
    if (
        not _valid_sha256(value.exact_sha256)
        or type(value.prefix_sha256) is not tuple
        or len(value.prefix_sha256) > _MAX_PREFIXES
        or not all(_valid_sha256(item) for item in value.prefix_sha256)
        or not _valid_sha256(value._request_source_sha256)
        or type(value._request_placeholders) is not tuple
        or not all(type(item) is str for item in value._request_placeholders)
        or not _valid_privacy_discriminator(
            value._request_privacy_discriminator
        )
        or (
            value._request_privacy_discriminator is None
        ) != (len(value._request_placeholders) == 0)
        or type(value._prefix_source_sha256) is not tuple
        or len(value._prefix_source_sha256) != len(value.prefix_sha256)
        or not all(_valid_sha256(item) for item in value._prefix_source_sha256)
        or type(value._prefix_lineage) is not tuple
        or len(value._prefix_lineage) != len(value.prefix_sha256)
        or type(value._prefix_privacy_discriminator) is not tuple
        or len(value._prefix_privacy_discriminator)
        != len(value.prefix_sha256)
        or not all(
            _valid_privacy_discriminator(item)
            for item in value._prefix_privacy_discriminator
        )
        or type(value._cacheable) is not bool
        or type(value._signature) is not bytes
    ):
        return False
    expected = _key_set_signature(
        exact_hash=value.exact_sha256,
        prefix_hashes=value.prefix_sha256,
        request_source_sha256=value._request_source_sha256,
        request_lineage=value._request_lineage,
        request_placeholders=value._request_placeholders,
        request_privacy_discriminator=value._request_privacy_discriminator,
        prefix_source_sha256=value._prefix_source_sha256,
        prefix_lineage=value._prefix_lineage,
        prefix_privacy_discriminator=value._prefix_privacy_discriminator,
        cacheable=value._cacheable,
    )
    return hmac.compare_digest(value._signature, expected)


def _valid_candidate(value: object) -> bool:
    if type(value) is not CacheCandidate:
        return False
    if (
        not _valid_sha256(value._exact_sha256)
        or type(value._prefix_sha256) is not tuple
        or len(value._prefix_sha256) > _MAX_PREFIXES
        or not all(_valid_sha256(item) for item in value._prefix_sha256)
        or type(value._redacted_response) is not str
        or not _valid_sha256(value._response_sha256)
        or _sha256_text(value._redacted_response) != value._response_sha256
        or value._provider_metadata_json not in _ALLOWED_METADATA_JSON
        or not _valid_sha256(value._request_source_sha256)
        or not _valid_sha256(value._response_source_sha256)
        or not _valid_sha256(value._completion_body_sha256)
        or not _valid_privacy_discriminator(value._privacy_discriminator)
        or type(value._signature) is not bytes
    ):
        return False
    expected = _candidate_signature(
        exact_hash=value._exact_sha256,
        prefix_hashes=value._prefix_sha256,
        redacted_response=value._redacted_response,
        response_sha256=value._response_sha256,
        provider_metadata_json=value._provider_metadata_json,
        request_source_sha256=value._request_source_sha256,
        response_source_sha256=value._response_source_sha256,
        completion_body_sha256=value._completion_body_sha256,
        privacy_discriminator=value._privacy_discriminator,
    )
    return hmac.compare_digest(value._signature, expected)


def _lineage_token(value: object) -> str:
    return hmac.new(
        _ADMISSION_KEY,
        f"lineage\0{id(value)}".encode("ascii"),
        "sha256",
    ).hexdigest()


def _verified_redacted_text(
    value: object, provenance: _RedactionProvenance
) -> str:
    text = getattr(value, "redacted_text", None)
    if (
        type(text) is not str
        or _sha256_text(text) != provenance.redacted_sha256
    ):
        raise ValueError(_ENTRY_ERROR)
    return text


def _redaction_provenance(value: object) -> _RedactionProvenance | None:
    try:
        from .pii import _redaction_provenance as provenance_for

        raw = provenance_for(value)
    except (ImportError, TypeError, ValueError):
        return None
    if raw is None:
        return None
    try:
        provenance = _RedactionProvenance(
            role=raw.role,
            lineage=raw.lineage,
            source_sha256=raw.source_sha256,
            redacted_sha256=raw.redacted_sha256,
            mapping_placeholders=frozenset(raw.mapping_placeholders),
            used_placeholders=frozenset(raw.used_placeholders),
            cacheable=raw.cacheable,
            privacy_discriminator=raw.privacy_discriminator,
        )
    except (AttributeError, TypeError):
        return None
    if (
        provenance.role not in {"request", "response"}
        or not _valid_sha256(provenance.source_sha256)
        or not _valid_sha256(provenance.redacted_sha256)
        or type(provenance.cacheable) is not bool
        or not _valid_privacy_discriminator(
            provenance.privacy_discriminator
        )
        or (
            provenance.privacy_discriminator is None
        ) != (len(provenance.mapping_placeholders) == 0)
        or not all(
            type(item) is str for item in provenance.mapping_placeholders
        )
        or not all(type(item) is str for item in provenance.used_placeholders)
    ):
        return None
    return provenance


def _stream_provenance(value: object) -> _StreamProvenance | None:
    try:
        from .streaming import _stream_provenance as provenance_for

        raw = provenance_for(value)
    except (ImportError, TypeError, ValueError):
        return None
    if raw is None:
        return None
    try:
        provenance = _StreamProvenance(
            status=raw.status,
            cacheable=raw.cacheable,
            has_tool_use=raw.has_tool_use,
            text_sha256=raw.text_sha256,
            canonical_body_sha256=raw.canonical_body_sha256,
        )
    except (AttributeError, TypeError, ValueError):
        return None
    text = getattr(value, "text", None)
    canonical_body = getattr(value, "canonical_body", None)
    if (
        provenance.status not in {"complete", "interrupted", "uncertain"}
        or type(provenance.cacheable) is not bool
        or type(provenance.has_tool_use) is not bool
        or not _valid_sha256(provenance.text_sha256)
        or not _valid_sha256(provenance.canonical_body_sha256)
        or type(text) is not str
        or type(canonical_body) is not bytes
        or _sha256_text(text) != provenance.text_sha256
        or hashlib.sha256(canonical_body).hexdigest()
        != provenance.canonical_body_sha256
    ):
        return None
    return provenance


def _sha256_text(value: str) -> str:
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except (AttributeError, UnicodeError):
        raise ValueError(_ENTRY_ERROR) from None


def _valid_sha256(value: object) -> bool:
    return type(value) is str and _HEX_SHA256.fullmatch(value) is not None


def _valid_privacy_discriminator(value: object) -> bool:
    return value is None or _valid_sha256(value)


def _exact_entry_size(
    key_hash: str, response: str, response_sha256: str
) -> int:
    try:
        return len(key_hash.encode("ascii")) + len(
            response.encode("utf-8")
        ) + len(response_sha256.encode("ascii"))
    except (AttributeError, UnicodeError):
        raise ValueError(_ENTRY_ERROR) from None


def _prefix_entry_size(
    prefix_hash: str,
    source_key_hash: str,
    depth: int,
    metadata_json: str,
) -> int:
    try:
        return (
            len(prefix_hash.encode("ascii"))
            + len(source_key_hash.encode("ascii"))
            + len(str(depth).encode("ascii"))
            + len(metadata_json.encode("utf-8"))
        )
    except (AttributeError, UnicodeError):
        raise ValueError(_ENTRY_ERROR) from None


def _stored_size(connection: sqlite3.Connection) -> int:
    exact = int(
        connection.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM exact_entries"
        ).fetchone()[0]
    )
    prefixes = int(
        connection.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM prefix_entries"
        ).fetchone()[0]
    )
    return exact + prefixes


def _coerce_database_path(value: str | Path) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError, OSError):
        raise ValueError(_PATH_ERROR) from None
    if path.name != _DATABASE_FILENAME:
        raise ValueError(_PATH_ERROR)
    return path


def _prepare_private_database(path: Path) -> _DatabaseIdentity:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if _WINDOWS_FILE_SECURITY is not None:
        _WINDOWS_FILE_SECURITY.harden_directory(path.parent)
    parent = _private_parent_identity(path.parent)
    _verify_sidecars(path)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and not _private_regular(existing):
        raise ValueError(_PATH_ERROR)

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    binary = getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | nofollow | binary, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not _private_regular(metadata):
            raise ValueError(_PATH_ERROR)
        if _WINDOWS_FILE_SECURITY is not None:
            _WINDOWS_FILE_SECURITY.harden_file(descriptor)
        else:
            _fchmod_private(descriptor, path, metadata)
        after = path.lstat()
        if not _same_inode(metadata, after) or not _private_regular(after):
            raise ValueError(_PATH_ERROR)
    finally:
        os.close(descriptor)

    parent_after = _private_parent_identity(path.parent)
    if parent_after != parent:
        raise ValueError(_PATH_ERROR)
    return _DatabaseIdentity(
        parent_device=parent[0],
        parent_inode=parent[1],
        database_device=int(after.st_dev),
        database_inode=int(after.st_ino),
    )


def _private_parent_identity(path: Path) -> tuple[int, int]:
    try:
        metadata = path.lstat()
    except OSError:
        raise ValueError(_PATH_ERROR) from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(_PATH_ERROR)
    if os.name != "nt":
        geteuid = getattr(os, "geteuid", None)
        if (
            callable(geteuid)
            and int(metadata.st_uid) != int(geteuid())
        ):
            raise ValueError(_PATH_ERROR)
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError(_PATH_ERROR)
    return int(metadata.st_dev), int(metadata.st_ino)


def _verify_database_identity(path: Path, identity: _DatabaseIdentity) -> None:
    parent = _private_parent_identity(path.parent)
    if parent != (identity.parent_device, identity.parent_inode):
        raise ValueError(_PATH_ERROR)
    try:
        metadata = path.lstat()
    except OSError:
        raise ValueError(_PATH_ERROR) from None
    if (
        not _private_regular(metadata)
        or (int(metadata.st_dev), int(metadata.st_ino))
        != (identity.database_device, identity.database_inode)
    ):
        raise ValueError(_PATH_ERROR)


def _verify_sidecars(path: Path) -> None:
    for candidate in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise ValueError(_PATH_ERROR) from None
        if not _private_regular(metadata):
            raise ValueError(_PATH_ERROR)


def _verify_private_file(
    path: Path,
    *,
    expected: tuple[int, int] | None,
    required: bool,
) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if required:
            raise ValueError(_PATH_ERROR) from None
        return
    except OSError:
        raise ValueError(_PATH_ERROR) from None
    if not _private_regular(metadata):
        raise ValueError(_PATH_ERROR)
    if expected is not None and (
        int(metadata.st_dev), int(metadata.st_ino)
    ) != expected:
        raise ValueError(_PATH_ERROR)
    geteuid = getattr(os, "geteuid", None)
    if (
        stat.S_IMODE(metadata.st_mode) != 0o600
        or (callable(geteuid) and int(metadata.st_uid) != int(geteuid()))
    ):
        raise ValueError(_PATH_ERROR)


def _tighten_private_file(
    path: Path,
    *,
    expected: tuple[int, int] | None,
    required: bool,
) -> None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        if required:
            raise ValueError(_PATH_ERROR) from None
        return
    except OSError:
        raise ValueError(_PATH_ERROR) from None
    if not _private_regular(before):
        raise ValueError(_PATH_ERROR)
    if expected is not None and (
        int(before.st_dev), int(before.st_ino)
    ) != expected:
        raise ValueError(_PATH_ERROR)
    if _WINDOWS_FILE_SECURITY is not None:
        return

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    binary = getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow | binary)
    except FileNotFoundError:
        if required:
            raise ValueError(_PATH_ERROR) from None
        return
    except OSError:
        raise ValueError(_PATH_ERROR) from None
    try:
        opened = os.fstat(descriptor)
        if not _private_regular(opened) or not _same_inode(before, opened):
            raise ValueError(_PATH_ERROR)
        _fchmod_private(descriptor, path, opened)
    finally:
        os.close(descriptor)


def _fchmod_private(descriptor: int, path: Path, opened: os.stat_result) -> None:
    fchmod = getattr(os, "fchmod", None)
    if callable(fchmod):
        fchmod(descriptor, 0o600)
        return
    if os.chmod in os.supports_follow_symlinks:
        os.chmod(path, 0o600, follow_symlinks=False)
    else:
        current = path.lstat()
        if not _same_inode(opened, current) or not _private_regular(current):
            raise ValueError(_PATH_ERROR)
        os.chmod(path, 0o600)


def _private_regular(metadata: os.stat_result) -> bool:
    return stat.S_ISREG(metadata.st_mode) and int(metadata.st_nlink) == 1


def _same_inode(first: os.stat_result, second: os.stat_result) -> bool:
    return (int(first.st_dev), int(first.st_ino)) == (
        int(second.st_dev),
        int(second.st_ino),
    )


def _database_objects(connection: sqlite3.Connection) -> set[tuple[str, str]]:
    return {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
        )
    }


def _validate_schema(connection: sqlite3.Connection) -> None:
    if _database_objects(connection) != _EXPECTED_OBJECTS:
        raise ValueError(_SCHEMA_ERROR)
    if _table_signature(connection, _EXACT_TABLE) != _EXACT_TABLE_SIGNATURE:
        raise ValueError(_SCHEMA_ERROR)
    if _table_signature(connection, _PREFIX_TABLE) != _PREFIX_TABLE_SIGNATURE:
        raise ValueError(_SCHEMA_ERROR)
    if not _strict_table(connection, _EXACT_TABLE) or not _strict_table(
        connection, _PREFIX_TABLE
    ):
        raise ValueError(_SCHEMA_ERROR)
    foreign_keys = tuple(connection.execute("PRAGMA foreign_key_list(prefix_entries)"))
    if len(foreign_keys) != 1:
        raise ValueError(_SCHEMA_ERROR)
    foreign_key = foreign_keys[0]
    if (
        str(foreign_key[2]) != _EXACT_TABLE
        or str(foreign_key[3]) != "source_key_hash"
        or str(foreign_key[4]) != "key_hash"
        or str(foreign_key[6]).upper() != "CASCADE"
    ):
        raise ValueError(_SCHEMA_ERROR)
    expected_indexes = {
        _EXACT_ACCESSED_INDEX: (_EXACT_TABLE, "accessed_at"),
        _EXACT_EXPIRES_INDEX: (_EXACT_TABLE, "expires_at"),
        _PREFIX_EXPIRES_INDEX: (_PREFIX_TABLE, "expires_at"),
    }
    for name, (table, column) in expected_indexes.items():
        if _single_column_index(connection, table, name) != column:
            raise ValueError(_SCHEMA_ERROR)


def _table_signature(
    connection: sqlite3.Connection, table: str
) -> tuple[tuple[object, ...], ...]:
    escaped = table.replace('"', '""')
    return tuple(
        (
            int(row[0]),
            str(row[1]),
            str(row[2]),
            int(row[3]),
            None if row[4] is None else str(row[4]),
            int(row[5]),
            int(row[6]),
        )
        for row in connection.execute(f'PRAGMA table_xinfo("{escaped}")')
    )


def _strict_table(connection: sqlite3.Connection, table: str) -> bool:
    rows = tuple(
        row
        for row in connection.execute("PRAGMA table_list")
        if str(row[1]) == table and str(row[2]) == "table"
    )
    return len(rows) == 1 and int(rows[0][5]) == 1


def _single_column_index(
    connection: sqlite3.Connection, table: str, name: str
) -> str | None:
    indexes = {
        str(row[1]): (int(row[2]), str(row[3]), int(row[4]))
        for row in connection.execute(f"PRAGMA index_list({table})")
    }
    if indexes.get(name) != (0, "c", 0):
        return None
    escaped = name.replace('"', '""')
    columns = tuple(
        row
        for row in connection.execute(f'PRAGMA index_xinfo("{escaped}")')
        if int(row[5]) == 1
    )
    if len(columns) != 1 or int(columns[0][3]) != 0:
        return None
    return str(columns[0][2])


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _stored_datetime(value: object) -> datetime | None:
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        normalized = parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized if _iso(normalized) == value else None


def _positive_int(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value <= _MAX_SQLITE_INTEGER
    )
