"""ExecutorPlugin interface and the CC Switch / OmniRoute implementations.

Executors are the *apply* half of the control plane: they report the active
target of an external provider manager, verify their local state, and attempt
a switch. Switching always fails closed: neither upstream exposes a verified
programmatic interface yet, so the default backends refuse with a stable error
code and a caller may inject a backend after live verification.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .cc_switch import DEFAULT_CC_SWITCH_DB, _read_only_connection
from .config import ID_PATTERN
from .omniroute import _default_usage_path


@dataclass(frozen=True)
class ExecutorState:
    plugin_id: str
    available: bool
    active_target: str | None
    detail: str


@dataclass(frozen=True)
class SwitchResult:
    plugin_id: str
    target: str
    ok: bool
    detail: str


class ExecutorPlugin(Protocol):
    plugin_id: str

    def state(self) -> ExecutorState: ...

    def verify(self) -> bool: ...

    def switch(self, target: str) -> SwitchResult: ...


def _valid_target(target: str) -> None:
    if not isinstance(target, str) or ID_PATTERN.fullmatch(target) is None:
        raise ValueError("executor target must be a stable identifier")


class CcSwitchExecutor:
    """Report and (currently) only verify the CC Switch Codex provider."""

    plugin_id = "cc_switch"

    def __init__(self, *, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_CC_SWITCH_DB

    def _active_codex_provider(self) -> str | None:
        if not self.db_path.is_file():
            return None
        try:
            connection = _read_only_connection(self.db_path)
        except (OSError, sqlite3.Error, ValueError):
            return None
        try:
            row = connection.execute(
                "SELECT name FROM providers "
                "WHERE app_type = 'codex' AND is_current = 1 "
                "ORDER BY sort_index LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        if row is None or not isinstance(row[0], str) or not row[0]:
            return None
        return row[0]

    def state(self) -> ExecutorState:
        active = self._active_codex_provider()
        available = self.db_path.is_file()
        return ExecutorState(
            plugin_id=self.plugin_id,
            available=available,
            active_target=active,
            detail=(
                f"CC Switch active Codex provider: {active}"
                if active is not None
                else "CC Switch database unavailable or no active Codex provider"
            ),
        )

    def verify(self) -> bool:
        return self._active_codex_provider() is not None

    def switch(self, target: str) -> SwitchResult:
        _valid_target(target)
        return SwitchResult(
            plugin_id=self.plugin_id,
            target=target,
            ok=False,
            detail="cc_switch_has_no_programmatic_interface",
        )


class OmniRouteExecutor:
    """Report and (currently) only verify OmniRoute local usage state."""

    plugin_id = "omniroute"

    def __init__(
        self,
        *,
        usage_path: Path | None = None,
        switch_backend: Callable[[str], bool] | None = None,
    ) -> None:
        self.usage_path = (
            Path(usage_path) if usage_path is not None else _default_usage_path()
        )
        self.switch_backend = switch_backend

    def state(self) -> ExecutorState:
        available = self.usage_path.is_file()
        return ExecutorState(
            plugin_id=self.plugin_id,
            available=available,
            active_target=None,
            detail=(
                "OmniRoute local usage file present; active target unverified"
                if available
                else "OmniRoute local usage file unavailable"
            ),
        )

    def verify(self) -> bool:
        return self.usage_path.is_file()

    def switch(self, target: str) -> SwitchResult:
        _valid_target(target)
        if self.switch_backend is None:
            return SwitchResult(
                plugin_id=self.plugin_id,
                target=target,
                ok=False,
                detail="omniroute_switch_backend_unavailable",
            )
        try:
            ok = bool(self.switch_backend(target))
        except Exception:
            ok = False
        return SwitchResult(
            plugin_id=self.plugin_id,
            target=target,
            ok=ok,
            detail="omniroute_switch_completed" if ok else "omniroute_switch_failed",
        )


def default_executors() -> tuple[ExecutorPlugin, ...]:
    return (CcSwitchExecutor(), OmniRouteExecutor())


def apply_executor_switch(
    executor: ExecutorPlugin,
    target: str,
    *,
    enabled: bool,
) -> SwitchResult:
    """Route Decision auto-apply gate: disabled by default, fails closed."""
    _valid_target(target)
    if not enabled:
        return SwitchResult(
            plugin_id=executor.plugin_id,
            target=target,
            ok=False,
            detail="auto_apply_disabled",
        )
    return executor.switch(target)
