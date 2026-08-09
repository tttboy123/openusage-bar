"""Deterministic, request-local PII redaction for Gateway caching.

The redacted text is safe to use as cache-key/cache-value input.  Original
values remain only in the in-memory request lineage so a cache hit can be
rehydrated for that request; they are intentionally absent from repr/public
serialization and from the provenance view consumed by ``gateway.cache``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping


MAX_REDACTION_BYTES = 16 * 1024 * 1024
MAX_LITERAL_SECRETS = 128
MAX_LITERAL_SECRET_BYTES = 4 * 1024

_RESULT_SEAL = object()
_INVALID_RESULT_ERROR = "invalid redaction result"
_PROCESS_PRIVACY_KEY = os.urandom(32)
_PRIVACY_CONTEXT_DOMAIN = b"openusage-gateway-privacy-context/v1\0"
_RESULT_INTEGRITY_DOMAIN = b"openusage-gateway-redaction-result/v1\0"
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))
_HEX_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_RESERVED = re.compile(r"__OPENUSAGE_[A-Z][A-Z0-9]*_[1-9][0-9]*__")
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
    r"(?![A-Za-z0-9-])"
)
_POSIX_USER_PATH = re.compile(
    r"(?<![A-Za-z0-9_])/(?:Users|home)/"
    r"[^/\s\"'`<>]+(?:/[^\s\"'`<>]*)?"
)
_WINDOWS_USER_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]+Users[\\/]+"
    r"[^\\/\s\"'<>|]+(?:[\\/]+[^\s\"'<>|]*)?"
)
_PHONE = re.compile(
    r"(?<![A-Za-z0-9])(?:\+[1-9]\d{0,2}[ .-]?)?"
    r"\(?\d{3}\)?[ .-]?\d{3}[ .-]\d{4}(?![A-Za-z0-9])"
)
_BEARER = re.compile(
    r"(?i)(?<![A-Za-z0-9])Bearer[ \t]+[A-Za-z0-9._~+/=-]{8,}"
)
_API_KEY_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{6,}"),
    re.compile(r"(?<![A-Za-z0-9])AIza[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])AKIA[A-Z0-9]{16}(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])(?:glpat-|hf_|npm_)[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}"),
)
_HIGH_ENTROPY = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z0-9][A-Za-z0-9_+/=-]{31,}"
    r"(?![A-Za-z0-9_])"
)


@dataclass(frozen=True, repr=False)
class _RedactionProvenance:
    lineage: object
    role: str
    redacted_text: str
    bypass_reason: str | None
    source_sha256: str
    redacted_sha256: str
    cacheable: bool
    privacy_discriminator: str | None
    mapping_placeholders: frozenset[str]
    used_placeholders: frozenset[str]

    def __repr__(self) -> str:
        return (
            "_RedactionProvenance("
            f"role={self.role!r}, cacheable={self.cacheable!r}, "
            f"placeholder_count={len(self.mapping_placeholders)!r})"
        )


@dataclass(frozen=True, repr=False)
class _ValidatedResultState:
    provenance: _RedactionProvenance
    mapping_items: tuple[tuple[str, str], ...]

    def __repr__(self) -> str:
        return "_ValidatedResultState(<private>)"


@dataclass(frozen=True)
class _Match:
    start: int
    end: int
    kind: str
    value: str
    priority: int
    bypass_reason: str | None
    existing: bool = False
    replace: bool = True


class RedactionResult:
    """Opaque, sealed result carrying one request-local rehydration map."""

    __slots__ = (
        "_bypass_reason",
        "_cacheable",
        "_lineage",
        "_mapping",
        "_redacted_text",
        "_role",
        "_seal",
        "_source_sha256",
        "_integrity",
        "_used_placeholders",
    )

    def __init__(
        self,
        *,
        redacted_text: str,
        cacheable: bool,
        bypass_reason: str | None,
        mapping: Mapping[str, str],
        lineage: object,
        role: str,
        source_sha256: str,
        used_placeholders: frozenset[str],
        _seal: object,
    ) -> None:
        if _seal is not _RESULT_SEAL:
            raise TypeError("RedactionResult instances are created by Redactor")
        self._seal = _RESULT_SEAL
        self._redacted_text = redacted_text
        self._cacheable = cacheable
        self._bypass_reason = bypass_reason
        self._mapping = MappingProxyType(dict(mapping))
        self._lineage = lineage
        self._role = role
        self._source_sha256 = source_sha256
        self._used_placeholders = used_placeholders
        self._integrity = _result_integrity(
            redacted_text=redacted_text,
            cacheable=cacheable,
            bypass_reason=bypass_reason,
            mapping=self._mapping,
            lineage=lineage,
            role=role,
            source_sha256=source_sha256,
            used_placeholders=used_placeholders,
        )

    @property
    def redacted_text(self) -> str:
        return _require_redaction_state(self).provenance.redacted_text

    @property
    def cacheable(self) -> bool:
        return _require_redaction_state(self).provenance.cacheable

    @property
    def bypass_reason(self) -> str | None:
        return _require_redaction_state(self).provenance.bypass_reason

    @property
    def placeholder_count(self) -> int:
        return len(
            _require_redaction_state(self).provenance.mapping_placeholders
        )

    def rehydrate(self, text: str) -> str:
        state = _require_redaction_state(self)
        _validated_text(text)
        result = text
        # Placeholders never overlap, but longest-first keeps this correct if a
        # future version introduces a typed suffix with a shared prefix.
        ordered = sorted(
            state.mapping_items,
            key=lambda item: len(item[0]),
            reverse=True,
        )
        for placeholder, original in ordered:
            result = result.replace(placeholder, original)
        return result

    def redact_response(self, text: str) -> "RedactionResult":
        state = _require_redaction_state(self)
        provenance = state.provenance
        if provenance.role != "request":
            raise ValueError("response redaction requires a request result")
        source = _validated_text(text)
        mapping = dict(state.mapping_items)
        reverse = {
            original: placeholder
            for placeholder, original in state.mapping_items
        }
        return _redact_text(
            source,
            literal_secrets=(),
            lineage=provenance.lineage,
            role="response",
            existing_mapping=mapping,
            existing_reverse=reverse,
            inherited_reason=provenance.bypass_reason,
            inherited_cacheable=provenance.cacheable,
        )

    def to_public_dict(self) -> dict[str, object]:
        provenance = _require_redaction_state(self).provenance
        return {
            "cacheable": provenance.cacheable,
            "bypassReason": provenance.bypass_reason,
            "placeholderCount": len(provenance.mapping_placeholders),
        }

    def __repr__(self) -> str:
        try:
            state = _validated_result_state(self)
            if state is None:
                return "RedactionResult(<invalid>)"
            provenance = state.provenance
            return (
                "RedactionResult("
                f"role={provenance.role!r}, "
                f"cacheable={provenance.cacheable!r}, "
                f"bypass_reason={provenance.bypass_reason!r}, "
                "placeholder_count="
                f"{len(provenance.mapping_placeholders)!r})"
            )
        except BaseException:
            return "RedactionResult(<invalid>)"


class Redactor:
    """Create a fresh deterministic redaction lineage for each request."""

    __slots__ = ("_literal_secrets",)

    def __init__(self, literal_secrets: Iterable[str] = ()) -> None:
        if isinstance(literal_secrets, (str, bytes, bytearray)):
            raise ValueError("literal secrets must be a bounded sequence")
        try:
            values = tuple(literal_secrets)
        except TypeError:
            raise ValueError("literal secrets must be a bounded sequence") from None
        if len(values) > MAX_LITERAL_SECRETS:
            raise ValueError("literal secrets must be a bounded sequence")
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            if type(value) is not str:
                raise ValueError("literal secrets must be bounded text")
            encoded = _encoded(value)
            if not value or len(encoded) > MAX_LITERAL_SECRET_BYTES:
                raise ValueError("literal secrets must be bounded text")
            if value not in seen:
                seen.add(value)
                normalized.append(value)
        self._literal_secrets = tuple(normalized)

    def redact(self, text: str) -> RedactionResult:
        source = _validated_text(text)
        return _redact_text(
            source,
            literal_secrets=self._literal_secrets,
            lineage=object(),
            role="request",
            existing_mapping={},
            existing_reverse={},
            inherited_reason=None,
            inherited_cacheable=True,
        )

    def __repr__(self) -> str:
        return f"Redactor(literal_secret_count={len(self._literal_secrets)!r})"


def _validated_result_state(value: object) -> _ValidatedResultState | None:
    """Validate direct slot snapshots without calling any public boundary."""

    if type(value) is not RedactionResult:
        return None
    try:
        seal = value._seal
        redacted_text = value._redacted_text
        cacheable = value._cacheable
        bypass_reason = value._bypass_reason
        mapping = value._mapping
        lineage = value._lineage
        role = value._role
        source_sha256 = value._source_sha256
        used_placeholders = value._used_placeholders
        integrity = value._integrity
        if (
            seal is not _RESULT_SEAL
            or type(redacted_text) is not str
            or len(_encoded(redacted_text)) > MAX_REDACTION_BYTES
            or type(cacheable) is not bool
            or (
                bypass_reason is not None
                and type(bypass_reason) is not str
            )
            or type(mapping) is not _MAPPING_PROXY_TYPE
            or type(lineage) is not object
            or type(role) is not str
            or role not in {"request", "response"}
            or type(source_sha256) is not str
            or _HEX_SHA256.fullmatch(source_sha256) is None
            or type(used_placeholders) is not frozenset
            or not all(
                type(placeholder) is str
                and _RESERVED.fullmatch(placeholder) is not None
                and type(original) is str
                for placeholder, original in mapping.items()
            )
            or not all(
                type(placeholder) is str
                for placeholder in used_placeholders
            )
            or not used_placeholders.issubset(mapping)
            or type(integrity) is not bytes
        ):
            return None
        privacy_discriminator = _privacy_discriminator(mapping)
        expected_integrity = _result_integrity(
            redacted_text=redacted_text,
            cacheable=cacheable,
            bypass_reason=bypass_reason,
            mapping=mapping,
            lineage=lineage,
            role=role,
            source_sha256=source_sha256,
            used_placeholders=used_placeholders,
        )
        if not hmac.compare_digest(integrity, expected_integrity):
            return None
        redacted_sha256 = _digest(redacted_text)
        mapping_items = tuple(mapping.items())
    except (AttributeError, TypeError, UnicodeError, ValueError):
        return None
    provenance = _RedactionProvenance(
        lineage=lineage,
        role=role,
        redacted_text=redacted_text,
        bypass_reason=bypass_reason,
        source_sha256=source_sha256,
        redacted_sha256=redacted_sha256,
        cacheable=cacheable,
        privacy_discriminator=privacy_discriminator,
        mapping_placeholders=frozenset(
            placeholder for placeholder, _ in mapping_items
        ),
        used_placeholders=used_placeholders,
    )
    return _ValidatedResultState(
        provenance=provenance,
        mapping_items=mapping_items,
    )


def _redaction_provenance(value: object) -> _RedactionProvenance | None:
    """Return secret-free provenance for the cache module, or ``None``."""

    state = _validated_result_state(value)
    return None if state is None else state.provenance


def _require_redaction_state(value: object) -> _ValidatedResultState:
    try:
        state = _validated_result_state(value)
    except Exception:
        state = None
    if state is None:
        raise ValueError(_INVALID_RESULT_ERROR) from None
    return state


def _redact_text(
    source: str,
    *,
    literal_secrets: tuple[str, ...],
    lineage: object,
    role: str,
    existing_mapping: Mapping[str, str],
    existing_reverse: Mapping[str, str],
    inherited_reason: str | None,
    inherited_cacheable: bool,
) -> RedactionResult:
    matches = _matches(source, literal_secrets, existing_reverse)
    selected = _select_non_overlapping(matches)
    mapping = dict(existing_mapping)
    reverse = dict(existing_reverse)
    occupied = set(_RESERVED.findall(source)) | set(mapping)
    counters: dict[str, int] = {}
    pieces: list[str] = []
    cursor = 0
    cacheable = inherited_cacheable
    bypass_reason = inherited_reason
    used: set[str] = set()

    if any(
        character not in "\t\n\r"
        and unicodedata.category(character).startswith("C")
        for character in source
    ):
        cacheable = False
        if bypass_reason is None:
            bypass_reason = "uncertain_text_encoding"

    for match in selected:
        pieces.append(source[cursor:match.start])
        if match.bypass_reason is not None:
            cacheable = False
            if bypass_reason is None:
                bypass_reason = match.bypass_reason
        if not match.replace:
            pieces.append(match.value)
        else:
            placeholder = reverse.get(match.value)
            if placeholder is None:
                placeholder = _next_placeholder(match.kind, counters, occupied)
                mapping[placeholder] = match.value
                reverse[match.value] = placeholder
                occupied.add(placeholder)
                if role == "response":
                    cacheable = False
                    if bypass_reason is None:
                        bypass_reason = "response_contains_new_private_data"
            pieces.append(placeholder)
            used.add(placeholder)
        cursor = match.end
    pieces.append(source[cursor:])
    redacted = "".join(pieces)

    # Any placeholder present in upstream text is a collision, even if it
    # happens to name a placeholder valid for this request.  Generated
    # placeholders are added only after this source-level check.
    if _RESERVED.search(source):
        cacheable = False
        if bypass_reason is None:
            bypass_reason = "reserved_placeholder_collision"

    return RedactionResult(
        redacted_text=redacted,
        cacheable=cacheable,
        bypass_reason=bypass_reason,
        mapping=mapping,
        lineage=lineage,
        role=role,
        source_sha256=_digest(source),
        used_placeholders=frozenset(used),
        _seal=_RESULT_SEAL,
    )


def _matches(
    text: str,
    literal_secrets: tuple[str, ...],
    existing_reverse: Mapping[str, str],
) -> list[_Match]:
    result: list[_Match] = []

    for original in existing_reverse:
        start = 0
        while True:
            index = text.find(original, start)
            if index < 0:
                break
            result.append(
                _Match(index, index + len(original), "PRIVATE", original, 0, None, True)
            )
            start = index + max(1, len(original))

    for match in _RESERVED.finditer(text):
        result.append(
            _Match(
                match.start(),
                match.end(),
                "COLLISION",
                match.group(0),
                1,
                "reserved_placeholder_collision",
                replace=False,
            )
        )

    for literal in literal_secrets:
        start = 0
        while True:
            index = text.find(literal, start)
            if index < 0:
                break
            result.append(
                _Match(
                    index,
                    index + len(literal),
                    "LITERAL",
                    literal,
                    2,
                    "literal_secret_detected",
                )
            )
            start = index + max(1, len(literal))

    for pattern in (_BEARER, *_API_KEY_PATTERNS):
        for match in pattern.finditer(text):
            result.append(
                _Match(
                    match.start(),
                    match.end(),
                    "CREDENTIAL",
                    match.group(0),
                    3,
                    "credential_detected",
                )
            )

    for pattern, kind in (
        (_EMAIL, "EMAIL"),
        (_WINDOWS_USER_PATH, "PATH"),
        (_POSIX_USER_PATH, "PATH"),
        (_PHONE, "PHONE"),
    ):
        for match in pattern.finditer(text):
            result.append(
                _Match(
                    match.start(),
                    match.end(),
                    kind,
                    match.group(0),
                    10,
                    None,
                )
            )

    for match in _HIGH_ENTROPY.finditer(text):
        value = match.group(0)
        if _looks_high_entropy(value):
            result.append(
                _Match(
                    match.start(),
                    match.end(),
                    "SECRET",
                    value,
                    20,
                    "unclassified_secret_detected",
                )
            )
    return result


def _select_non_overlapping(matches: list[_Match]) -> tuple[_Match, ...]:
    ordered = sorted(
        matches,
        key=lambda item: (
            item.start,
            0 if item.existing else 1,
            -(item.end - item.start),
            item.priority,
        ),
    )
    selected: list[_Match] = []
    cursor = -1
    for match in ordered:
        if match.start < cursor:
            continue
        selected.append(match)
        cursor = match.end
    return tuple(selected)


def _next_placeholder(
    kind: str,
    counters: dict[str, int],
    occupied: set[str],
) -> str:
    index = counters.get(kind, 0)
    while True:
        index += 1
        candidate = f"__OPENUSAGE_{kind}_{index}__"
        if candidate not in occupied:
            counters[kind] = index
            return candidate


def _looks_high_entropy(value: str) -> bool:
    classes = sum(
        (
            any(character.islower() for character in value),
            any(character.isupper() for character in value),
            any(character.isdigit() for character in value),
            any(not character.isalnum() for character in value),
        )
    )
    if classes < 3 or len(set(value)) < 12:
        return False
    counts = {character: value.count(character) for character in set(value)}
    entropy = -sum(
        (count / len(value)) * math.log2(count / len(value))
        for count in counts.values()
    )
    return entropy >= 3.5


def _validated_text(value: object) -> str:
    if type(value) is not str:
        raise ValueError("redaction input must be bounded text")
    encoded = _encoded(value)
    if len(encoded) > MAX_REDACTION_BYTES:
        raise ValueError("redaction input must be bounded text")
    return value


def _encoded(value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeError:
        raise ValueError("redaction input must be bounded text") from None


def _privacy_discriminator(mapping: Mapping[str, str]) -> str | None:
    """Return a process-local, non-enumerable fingerprint of private context."""

    if not mapping:
        return None
    digest = hmac.new(
        _PROCESS_PRIVACY_KEY,
        _PRIVACY_CONTEXT_DOMAIN,
        hashlib.sha256,
    )
    for placeholder, original in sorted(mapping.items()):
        for component in (placeholder, original):
            encoded = _encoded(component)
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _result_integrity(
    *,
    redacted_text: str,
    cacheable: bool,
    bypass_reason: str | None,
    mapping: Mapping[str, str],
    lineage: object,
    role: str,
    source_sha256: str,
    used_placeholders: frozenset[str],
) -> bytes:
    privacy_discriminator = _privacy_discriminator(mapping)
    payload = json.dumps(
        {
            "bypass_reason": bypass_reason,
            "cacheable": cacheable,
            "lineage": id(lineage),
            "mapping": sorted(mapping.items()),
            "privacy_discriminator": privacy_discriminator,
            "redacted_text": redacted_text,
            "role": role,
            "source_sha256": source_sha256,
            "used_placeholders": sorted(used_placeholders),
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hmac.digest(
        _PROCESS_PRIVACY_KEY,
        _RESULT_INTEGRITY_DOMAIN + payload,
        "sha256",
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["RedactionResult", "Redactor"]
