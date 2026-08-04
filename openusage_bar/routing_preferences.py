"""Private, bounded preferences for the local Route Decision API."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .routing_contract import MAX_COUNTER, _stable_id


SCHEMA_VERSION = 1
MAX_DOCUMENT_BYTES = 16 * 1024
_KEYS = frozenset({
    "schemaVersion", "revision", "decisionApiEnabled", "defaultPolicyId",
})


class RoutingPreferencesConfigError(ValueError):
    """Routing preferences failed sanitized validation."""


@dataclass(frozen=True)
class RoutingPreferences:
    revision: int
    decision_api_enabled: bool
    default_policy_id: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or not 0 <= self.revision <= MAX_COUNTER
            or not isinstance(self.decision_api_enabled, bool)
        ):
            raise ValueError("invalid routing preferences")
        _stable_id("default_policy_id", self.default_policy_id)


def _invalid() -> RoutingPreferencesConfigError:
    return RoutingPreferencesConfigError("invalid routing preferences configuration")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


class RoutingPreferencesStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def defaults() -> RoutingPreferences:
        return RoutingPreferences(0, True, "reliable")

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
        try:
            descriptor = os.open(
                self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
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
                chunk = os.read(descriptor, min(remaining, 16 * 1024))
                if not chunk:
                    raise _invalid()
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise _invalid()
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def load(self) -> RoutingPreferences:
        if not os.path.lexists(self.path):
            return self.defaults()
        try:
            payload = json.loads(
                self._read().decode("utf-8"), object_pairs_hook=_strict_object
            )
            if not isinstance(payload, dict) or frozenset(payload) != _KEYS:
                raise _invalid()
            schema = payload["schemaVersion"]
            revision = payload["revision"]
            enabled = payload["decisionApiEnabled"]
            policy_id = payload["defaultPolicyId"]
            if (
                isinstance(schema, bool) or schema != SCHEMA_VERSION
                or isinstance(revision, bool) or not isinstance(revision, int)
                or not 0 <= revision <= MAX_COUNTER
                or not isinstance(enabled, bool)
            ):
                raise _invalid()
            return RoutingPreferences(revision, enabled, policy_id)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            if isinstance(error, RoutingPreferencesConfigError):
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

    def save(self, preferences: RoutingPreferences) -> None:
        try:
            if not isinstance(preferences, RoutingPreferences) or preferences.revision < 1:
                raise _invalid()
            if os.path.lexists(self.path) and preferences.revision <= self.load().revision:
                raise _invalid()
            encoded = (json.dumps({
                "schemaVersion": SCHEMA_VERSION,
                "revision": preferences.revision,
                "decisionApiEnabled": preferences.decision_api_enabled,
                "defaultPolicyId": preferences.default_policy_id,
            }, indent=2, sort_keys=True) + "\n").encode("utf-8")
            if len(encoded) > MAX_DOCUMENT_BYTES:
                raise _invalid()
            parent = self._prepare_parent()
            descriptor, temporary = tempfile.mkstemp(
                prefix="routing-preferences.", suffix=".json", dir=parent
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                os.chmod(self.path, 0o600)
                directory = os.open(
                    parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.lexists(temporary):
                    os.unlink(temporary)
        except (OSError, TypeError, ValueError) as error:
            if isinstance(error, RoutingPreferencesConfigError):
                raise
            raise _invalid() from error
