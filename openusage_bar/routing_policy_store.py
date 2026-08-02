"""Strict private persistence for user-defined routing policies."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .routing_contract import MAX_COUNTER
from .routing_policy import RoutePolicy, built_in_policy_ids


SCHEMA_VERSION = 1
MAX_POLICIES = 32
MAX_DOCUMENT_BYTES = 256 * 1024

_TOP_LEVEL_KEYS = frozenset({"schemaVersion", "revision", "policies"})
_POLICY_KEYS = frozenset({
    "policyId", "policyRevision",
    "reliabilityWeight", "headroomWeight", "latencyWeight", "costWeight",
    "minimumHeadroomBasisPoints", "maximumErrorRateBasisPoints",
    "minimumRuntimeSamples", "latencyReferenceMilliseconds",
    "costReferenceMicrounits", "costCurrency", "minimumBalanceMicrounits",
    "balanceReferenceMicrounits", "unknownPenaltyBasisPoints",
    "requireCost", "requireRuntime", "allowedExecutionClasses",
    "allowedPrivacyClasses",
})


class RoutingPolicyConfigError(ValueError):
    """Custom policy configuration failed sanitized validation."""


@dataclass(frozen=True)
class RoutingPolicyConfiguration:
    schema_version: int
    revision: int
    policies: tuple[RoutePolicy, ...]


def _invalid() -> RoutingPolicyConfigError:
    return RoutingPolicyConfigError("invalid routing policy configuration")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _exact(value: Any, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise _invalid()
    return value


def _integer(value: Any, *, minimum: int = 0, maximum: int = MAX_COUNTER) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _invalid()
    return value


def _strings(value: Any) -> frozenset[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 16
        or any(not isinstance(item, str) for item in value)
        or len(value) != len(set(value))
    ):
        raise _invalid()
    return frozenset(value)


def _decode_policy(value: Any) -> RoutePolicy:
    raw = _exact(value, _POLICY_KEYS)
    try:
        return RoutePolicy(
            policy_id=raw["policyId"],
            revision=raw["policyRevision"],
            reliability_weight=raw["reliabilityWeight"],
            headroom_weight=raw["headroomWeight"],
            latency_weight=raw["latencyWeight"],
            cost_weight=raw["costWeight"],
            min_headroom_bp=raw["minimumHeadroomBasisPoints"],
            max_error_rate_bp=raw["maximumErrorRateBasisPoints"],
            min_runtime_samples=raw["minimumRuntimeSamples"],
            latency_reference_ms=raw["latencyReferenceMilliseconds"],
            cost_reference_micros=raw["costReferenceMicrounits"],
            cost_currency=raw["costCurrency"],
            min_balance_micros=raw["minimumBalanceMicrounits"],
            balance_reference_micros=raw["balanceReferenceMicrounits"],
            unknown_penalty=raw["unknownPenaltyBasisPoints"],
            require_cost=raw["requireCost"],
            require_runtime=raw["requireRuntime"],
            allowed_execution_classes=_strings(raw["allowedExecutionClasses"]),
            allowed_privacy_classes=_strings(raw["allowedPrivacyClasses"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise _invalid() from error


def _policy_wire(value: RoutePolicy) -> dict[str, object]:
    return {
        "policyId": value.policy_id,
        "policyRevision": value.revision,
        "reliabilityWeight": value.reliability_weight,
        "headroomWeight": value.headroom_weight,
        "latencyWeight": value.latency_weight,
        "costWeight": value.cost_weight,
        "minimumHeadroomBasisPoints": value.min_headroom_bp,
        "maximumErrorRateBasisPoints": value.max_error_rate_bp,
        "minimumRuntimeSamples": value.min_runtime_samples,
        "latencyReferenceMilliseconds": value.latency_reference_ms,
        "costReferenceMicrounits": value.cost_reference_micros,
        "costCurrency": value.cost_currency,
        "minimumBalanceMicrounits": value.min_balance_micros,
        "balanceReferenceMicrounits": value.balance_reference_micros,
        "unknownPenaltyBasisPoints": value.unknown_penalty,
        "requireCost": value.require_cost,
        "requireRuntime": value.require_runtime,
        "allowedExecutionClasses": sorted(value.allowed_execution_classes),
        "allowedPrivacyClasses": sorted(value.allowed_privacy_classes),
    }


class RoutingPolicyStore:
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

    def load(self) -> RoutingPolicyConfiguration:
        if not os.path.lexists(self.path):
            return RoutingPolicyConfiguration(SCHEMA_VERSION, 0, ())
        try:
            payload = json.loads(
                self._read().decode("utf-8"), object_pairs_hook=_strict_object
            )
            raw = _exact(payload, _TOP_LEVEL_KEYS)
            if _integer(raw["schemaVersion"], minimum=1, maximum=1) != SCHEMA_VERSION:
                raise _invalid()
            revision = _integer(raw["revision"])
            values = raw["policies"]
            if not isinstance(values, list) or len(values) > MAX_POLICIES:
                raise _invalid()
            policies = tuple(_decode_policy(value) for value in values)
            self._validate_policies(policies)
            return RoutingPolicyConfiguration(
                SCHEMA_VERSION,
                revision,
                tuple(sorted(policies, key=lambda item: item.policy_id)),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            if isinstance(error, RoutingPolicyConfigError):
                raise
            raise _invalid() from error

    @staticmethod
    def _validate_policies(policies: tuple[RoutePolicy, ...]) -> None:
        if any(not isinstance(value, RoutePolicy) for value in policies):
            raise _invalid()
        ids = [value.policy_id for value in policies]
        if len(ids) != len(set(ids)) or set(ids) & built_in_policy_ids():
            raise _invalid()

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

    def save(self, policies: tuple[RoutePolicy, ...], *, revision: int) -> None:
        try:
            revision = _integer(revision, minimum=1)
            values = tuple(policies)
            if len(values) > MAX_POLICIES:
                raise _invalid()
            self._validate_policies(values)
            payload = {
                "schemaVersion": SCHEMA_VERSION,
                "revision": revision,
                "policies": [
                    _policy_wire(value)
                    for value in sorted(values, key=lambda item: item.policy_id)
                ],
            }
            encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
            if len(encoded) > MAX_DOCUMENT_BYTES:
                raise _invalid()
            parent = self._prepare_parent()
            if os.path.lexists(self.path):
                current = self.load().revision
                if revision <= current:
                    raise _invalid()
            descriptor, temporary = tempfile.mkstemp(
                prefix="routing-policies.", suffix=".json", dir=parent
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
            if isinstance(error, RoutingPolicyConfigError):
                raise
            raise _invalid() from error
