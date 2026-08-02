"""Strict, non-secret route target configuration persistence."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .routing_contract import MAX_COUNTER, RouteTarget


SCHEMA_VERSION = 1
MAX_TARGETS = 128
MAX_DOCUMENT_BYTES = 256 * 1024

_TOP_LEVEL_KEYS = frozenset({"schemaVersion", "revision", "targets"})
_TARGET_KEYS = frozenset({
    "targetId",
    "providerId",
    "accountRef",
    "modelId",
    "connectionRef",
    "executionClass",
    "executionAdapterId",
    "resourceMode",
    "factAccountRef",
    "runtimeScopeRef",
    "balanceCurrency",
    "costCurrency",
    "inputCostMicrosPerMillion",
    "outputCostMicrosPerMillion",
    "enabled",
    "regions",
    "privacyClass",
    "capabilities",
    "contextWindowTokens",
    "qualityTier",
})


class RouteTargetConfigError(ValueError):
    """The target document failed sanitized validation."""


@dataclass(frozen=True)
class RouteTargetConfiguration:
    schema_version: int
    revision: int
    targets: tuple[RouteTarget, ...]


def _invalid() -> RouteTargetConfigError:
    return RouteTargetConfigError("invalid route target configuration")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _exact_mapping(value: Any, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise _invalid()
    return value


def _integer(value: Any, *, minimum: int = 0, maximum: int = MAX_COUNTER) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _invalid()
    return value


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _invalid()
    return tuple(value)


def _decode_target(value: Any, available_adapters: frozenset[str]) -> RouteTarget:
    raw = _exact_mapping(value, _TARGET_KEYS)
    try:
        return RouteTarget(
            target_id=raw["targetId"],
            provider_id=raw["providerId"],
            account_ref=raw["accountRef"],
            model_id=raw["modelId"],
            connection_ref=raw["connectionRef"],
            execution_class=raw["executionClass"],
            execution_adapter_id=raw["executionAdapterId"],
            resource_mode=raw["resourceMode"],
            fact_account_ref=raw["factAccountRef"],
            runtime_scope_ref=raw["runtimeScopeRef"],
            balance_currency=raw["balanceCurrency"],
            cost_currency=raw["costCurrency"],
            input_cost_micros_per_million=raw["inputCostMicrosPerMillion"],
            output_cost_micros_per_million=raw["outputCostMicrosPerMillion"],
            enabled=raw["enabled"],
            adapter_available=raw["executionAdapterId"] in available_adapters,
            regions=_string_list(raw["regions"]),
            privacy_class=raw["privacyClass"],
            capabilities=_string_list(raw["capabilities"]),
            context_window_tokens=raw["contextWindowTokens"],
            quality_tier=raw["qualityTier"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise _invalid() from error


def _target_wire(value: RouteTarget) -> dict[str, Any]:
    return {
        "targetId": value.target_id,
        "providerId": value.provider_id,
        "accountRef": value.account_ref,
        "modelId": value.model_id,
        "connectionRef": value.connection_ref,
        "executionClass": value.execution_class,
        "executionAdapterId": value.execution_adapter_id,
        "resourceMode": value.resource_mode,
        "factAccountRef": value.fact_account_ref,
        "runtimeScopeRef": value.runtime_scope_ref,
        "balanceCurrency": value.balance_currency,
        "costCurrency": value.cost_currency,
        "inputCostMicrosPerMillion": value.input_cost_micros_per_million,
        "outputCostMicrosPerMillion": value.output_cost_micros_per_million,
        "enabled": value.enabled,
        "regions": list(value.regions),
        "privacyClass": value.privacy_class,
        "capabilities": list(value.capabilities),
        "contextWindowTokens": value.context_window_tokens,
        "qualityTier": value.quality_tier,
    }


class RouteTargetStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _safe_stat(self) -> os.stat_result:
        try:
            value = self.path.lstat()
        except OSError as error:
            raise _invalid() from error
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != os.getuid()
            or stat.S_IMODE(value.st_mode) != 0o600
            or value.st_size > MAX_DOCUMENT_BYTES
        ):
            raise _invalid()
        return value

    def _read(self) -> bytes:
        before = self._safe_stat()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags)
        except OSError as error:
            raise _invalid() from error
        try:
            after = os.fstat(descriptor)
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or not stat.S_ISREG(after.st_mode)
                or after.st_uid != os.getuid()
                or stat.S_IMODE(after.st_mode) != 0o600
                or after.st_size > MAX_DOCUMENT_BYTES
            ):
                raise _invalid()
            chunks: list[bytes] = []
            remaining = after.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    raise _invalid()
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise _invalid()
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def load(self, *, available_adapters: Iterable[str]) -> RouteTargetConfiguration:
        if not os.path.lexists(self.path):
            return RouteTargetConfiguration(SCHEMA_VERSION, 0, ())
        try:
            payload = json.loads(
                self._read().decode("utf-8"),
                object_pairs_hook=_strict_object,
            )
            raw = _exact_mapping(payload, _TOP_LEVEL_KEYS)
            if _integer(raw["schemaVersion"], minimum=1, maximum=1) != SCHEMA_VERSION:
                raise _invalid()
            revision = _integer(raw["revision"])
            values = raw["targets"]
            if not isinstance(values, list) or len(values) > MAX_TARGETS:
                raise _invalid()
            adapters = frozenset(available_adapters)
            targets = tuple(_decode_target(value, adapters) for value in values)
            ids = [value.target_id for value in targets]
            if len(ids) != len(set(ids)):
                raise _invalid()
            return RouteTargetConfiguration(
                SCHEMA_VERSION,
                revision,
                tuple(sorted(targets, key=lambda item: item.target_id)),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            if isinstance(error, RouteTargetConfigError):
                raise
            raise _invalid() from error

    def _prepare_parent(self) -> Path:
        parent = self.path.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            value = parent.lstat()
        except OSError as error:
            raise _invalid() from error
        if (
            not stat.S_ISDIR(value.st_mode)
            or value.st_uid != os.getuid()
            or stat.S_IMODE(value.st_mode) & 0o077
        ):
            raise _invalid()
        return parent

    def save(self, targets: tuple[RouteTarget, ...], *, revision: int) -> None:
        try:
            revision = _integer(revision)
            values = tuple(targets)
            if len(values) > MAX_TARGETS or any(not isinstance(value, RouteTarget) for value in values):
                raise _invalid()
            ids = [value.target_id for value in values]
            if len(ids) != len(set(ids)):
                raise _invalid()
            payload = {
                "schemaVersion": SCHEMA_VERSION,
                "revision": revision,
                "targets": [
                    _target_wire(value)
                    for value in sorted(values, key=lambda item: item.target_id)
                ],
            }
            encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
            if len(encoded) > MAX_DOCUMENT_BYTES:
                raise _invalid()
            parent = self._prepare_parent()
            if os.path.lexists(self.path):
                current = self.load(available_adapters=()).revision
                if revision <= current:
                    raise _invalid()
            descriptor, temporary = tempfile.mkstemp(
                prefix="routing-targets.", suffix=".json", dir=parent
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                os.chmod(self.path, 0o600)
                directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.lexists(temporary):
                    os.unlink(temporary)
        except (OSError, TypeError, ValueError) as error:
            if isinstance(error, RouteTargetConfigError):
                raise
            raise _invalid() from error
