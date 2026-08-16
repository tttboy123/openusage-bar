"""Process-local counters for two shared, explicitly instrumented boundaries."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import secrets
from threading import Lock


_MAX_ATTEMPT_COUNT = (1 << 64) - 1


@dataclass(frozen=True, repr=False)
class SharedClientBoundaryAttemptCounters:
    """Closed counts for bounded HTTP opens and headless keychain gets."""

    process_epoch_sha256: str
    bounded_http_open_attempts: int
    headless_keychain_get_attempts: int

    def __post_init__(self) -> None:
        if (
            type(self.process_epoch_sha256) is not str
            or len(self.process_epoch_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.process_epoch_sha256
            )
            or any(
                type(value) is not int or not 0 <= value <= _MAX_ATTEMPT_COUNT
                for value in (
                    self.bounded_http_open_attempts,
                    self.headless_keychain_get_attempts,
                )
            )
        ):
            raise ValueError("invalid shared client boundary counters")

    def __repr__(self) -> str:
        return "<SharedClientBoundaryAttemptCounters closed>"


_COUNTER_LOCK = Lock()
_PROCESS_EPOCH_SEED = secrets.token_bytes(32)
_bounded_http_open_attempts = 0
_headless_keychain_get_attempts = 0


def _reset_shared_client_boundary_after_fork() -> None:
    global _COUNTER_LOCK, _PROCESS_EPOCH_SEED
    global _bounded_http_open_attempts, _headless_keychain_get_attempts
    _COUNTER_LOCK = Lock()
    _PROCESS_EPOCH_SEED = secrets.token_bytes(32)
    _bounded_http_open_attempts = 0
    _headless_keychain_get_attempts = 0


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_shared_client_boundary_after_fork)


def shared_client_boundary_attempt_counters() -> SharedClientBoundaryAttemptCounters:
    """Return one atomic, identity-free process-local snapshot."""

    with _COUNTER_LOCK:
        return SharedClientBoundaryAttemptCounters(
            hashlib.sha256(
                _PROCESS_EPOCH_SEED + str(os.getpid()).encode("ascii")
            ).hexdigest(),
            _bounded_http_open_attempts,
            _headless_keychain_get_attempts,
        )


def _record_bounded_http_open_attempt() -> None:
    global _bounded_http_open_attempts
    with _COUNTER_LOCK:
        if _bounded_http_open_attempts >= _MAX_ATTEMPT_COUNT:
            raise RuntimeError("shared client boundary counter unavailable")
        _bounded_http_open_attempts += 1


def _record_headless_keychain_get_attempt() -> None:
    global _headless_keychain_get_attempts
    with _COUNTER_LOCK:
        if _headless_keychain_get_attempts >= _MAX_ATTEMPT_COUNT:
            raise RuntimeError("shared client boundary counter unavailable")
        _headless_keychain_get_attempts += 1


__all__ = [
    "SharedClientBoundaryAttemptCounters",
    "shared_client_boundary_attempt_counters",
]
