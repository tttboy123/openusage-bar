from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable

from .bounded_process import BoundedProcessError, run_bounded
from .models import Category, Overview, ProviderCard, ProviderStatus
from .provider_catalog import catalog


logger = logging.getLogger(__name__)
AUTO_TIMEOUT_SECONDS = 12
DIRECT_TIMEOUT_SECONDS = 40
MAX_EXPORT_BYTES = 16 * 1024 * 1024
MAX_EXPORT_SNAPSHOTS = 4096
MAX_EXPORT_STRING_LENGTH = 4096
MAX_EXPORT_FIELDS = 256
MAX_EXPORT_DEPTH = 8
CURSOR_CLI_DIRECTORIES = (
    "/Applications/Cursor.app/Contents/Resources/app/bin",
    os.path.expanduser("~/Applications/Cursor.app/Contents/Resources/app/bin"),
)
CHILD_CLI_DIRECTORIES = CURSOR_CLI_DIRECTORIES + (
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/bin"),
    os.path.expanduser("~/.npm/bin"),
    os.path.expanduser("~/.npm-global/bin"),
    os.path.expanduser("~/Library/pnpm"),
    os.path.expanduser("~/.bun/bin"),
    os.path.expanduser("~/.cargo/bin"),
    os.path.expanduser("~/Documents/Codex/devtools/npm/bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
)
_CHILD_ENVIRONMENT_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "LC_COLLATE",
        "LC_MONETARY",
        "LC_NUMERIC",
        "LC_TIME",
        "TZ",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
    }
)


def child_subprocess_environment(
    environment: dict[str, str] | None = None,
    path_exists: Callable[[str], bool] | None = None,
) -> dict[str, str]:
    """Return a credential-free child environment with Cursor's CLI available."""
    parent = environment if environment is not None else os.environ
    child = {
        key: value
        for key, value in parent.items()
        if key in _CHILD_ENVIRONMENT_KEYS
    }
    exists = path_exists or os.path.isdir
    parts = [part for part in child.get("PATH", "").split(os.pathsep) if part]
    discovered = [
        directory
        for directory in CHILD_CLI_DIRECTORIES
        if exists(directory) and directory not in parts
    ]
    child["PATH"] = os.pathsep.join(discovered + parts)
    return child


class OpenUsageExportError(RuntimeError):
    """A sanitized OpenUsage export failure safe for logs and UI state."""


def openusage_path() -> str:
    local = os.path.expanduser("~/.local/bin/openusage")
    return local if os.path.isfile(local) else (shutil.which("openusage") or local)


def _format_tokens(value: Any) -> str:
    numeric = float(value)
    if numeric >= 1_000_000:
        return f"{numeric / 1_000_000:.1f}M tokens"
    if numeric >= 1_000:
        return f"{numeric / 1_000:.1f}K tokens"
    return f"{int(numeric)} tokens"


def _metric_number(metric: Any, field: str = "used") -> float | int | None:
    if isinstance(metric, dict):
        value = metric.get(field)
    else:
        value = metric
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        return None
    return value


def _metric_percent_remaining(metric: Any) -> float | None:
    remaining = _metric_number(metric, "remaining")
    if remaining is None:
        used = _metric_number(metric, "used")
        remaining = None if used is None else 100.0 - float(used)
    return None if remaining is None else min(100.0, max(0.0, float(remaining)))


def _cursor_quota(
    metrics: dict[str, Any], message: Any
) -> tuple[str, str, float] | None:
    remaining = _metric_percent_remaining(metrics.get("plan_percent_used"))
    if remaining is None:
        return None
    plan = str(message).split("—", 1)[0].strip() if isinstance(message, str) else "Cursor"
    plan = plan or "Cursor"
    detail = plan if plan.lower().endswith("plan") else f"{plan} plan"
    spend = _metric_number(metrics.get("plan_spend"))
    limit = _metric_number(metrics.get("plan_limit_usd"), "limit")
    if spend is not None and limit is not None and float(limit) > 0:
        detail += f" · ${float(spend):g} / ${float(limit):g} spent"
    return f"{remaining:g}% remaining", detail, remaining


def _status(value: Any) -> ProviderStatus:
    normalized = str(value or "UNKNOWN").upper()
    aliases = {
        "AUTH_ERROR": ProviderStatus.AUTH,
        "AUTH_REQUIRED": ProviderStatus.AUTH,
        "RATE_LIMIT": ProviderStatus.RATE_LIMITED,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return ProviderStatus(normalized)
    except ValueError:
        return ProviderStatus.UNKNOWN


def _validate_export_shape(value: Any, depth: int = 0) -> None:
    if depth > MAX_EXPORT_DEPTH:
        raise ValueError("snapshot nesting is too deep")
    if isinstance(value, dict):
        if len(value) > MAX_EXPORT_FIELDS:
            raise ValueError("snapshot has too many fields")
        for key, nested in value.items():
            if not isinstance(key, str) or len(key) > MAX_EXPORT_STRING_LENGTH:
                raise ValueError("snapshot field is invalid")
            _validate_export_shape(nested, depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_EXPORT_SNAPSHOTS:
            raise ValueError("snapshot list is too large")
        for nested in value:
            _validate_export_shape(nested, depth + 1)
    elif isinstance(value, str) and len(value) > MAX_EXPORT_STRING_LENGTH:
        raise ValueError("snapshot string is too large")


class OpenUsageAdapter:
    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        runner=None,
        environment: dict[str, str] | None = None,
        path_exists: Callable[[str], bool] | None = None,
    ):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.runner = runner
        self.environment = dict(environment if environment is not None else os.environ)
        self.path_exists = path_exists or os.path.isdir

    def _subprocess_environment(self) -> dict[str, str]:
        return child_subprocess_environment(self.environment, self.path_exists)

    @staticmethod
    def parse(payload: dict[str, Any], now: datetime) -> Overview:
        raw_snapshots = payload.get("snapshots") or []
        if not isinstance(raw_snapshots, list) or len(raw_snapshots) > MAX_EXPORT_SNAPSHOTS:
            raise ValueError("invalid snapshot count")
        cards: list[ProviderCard] = []
        for raw in raw_snapshots:
            if not isinstance(raw, dict):
                continue
            _validate_export_shape(raw)
            raw_provider_id = raw.get("provider_id")
            if (
                not isinstance(raw_provider_id, str)
                or len(raw_provider_id) > MAX_EXPORT_STRING_LENGTH
            ):
                continue
            provider_id = raw_provider_id
            try:
                family = catalog.resolve(
                    provider_id, provider_id.replace("_", " ").title()
                )
            except ValueError:
                continue
            metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
            message = raw.get("message")
            if isinstance(message, str) and len(message) > MAX_EXPORT_STRING_LENGTH:
                raise ValueError("snapshot message is too large")
            primary: str | None = None
            detail: str | None = None
            remaining_percent: float | None = None
            if isinstance(message, str) and message.strip():
                primary = message.strip()
            elif (token_value := _metric_number(metrics.get("window_tokens"))) is not None:
                primary = _format_tokens(token_value)
                if (cost_value := _metric_number(metrics.get("window_cost"))) is not None:
                    detail = f"${float(cost_value):.4f}"
            else:
                detail = "No live quota returned"

            if provider_id == "cursor":
                cursor_quota = _cursor_quota(metrics, message)
                if cursor_quota is not None:
                    primary, detail, remaining_percent = cursor_quota

            cards.append(
                ProviderCard(
                    provider_id=provider_id,
                    name=family.display_name,
                    category={
                        "subscription": Category.SUBSCRIPTION,
                        "local_tool": Category.LOCAL,
                        "api": Category.API,
                    }[family.category],
                    status=_status(raw.get("status")),
                    primary=primary,
                    detail=detail,
                    remaining_percent=remaining_percent,
                    resets_at=None,
                    source="OpenUsage",
                    refreshed_at=now,
                    family_id=provider_id,
                    credential_source="openusage",
                    source_kind="openusage",
                )
            )
        return Overview(cards)

    def _export(self, source: str) -> Overview:
        timeout = (
            AUTO_TIMEOUT_SECONDS if source == "auto" else DIRECT_TIMEOUT_SECONDS
        )
        try:
            runner = self.runner or run_bounded
            options: dict[str, Any] = {}
            if self.runner is None:
                options = {
                    "stdout_limit": MAX_EXPORT_BYTES,
                    "stderr_limit": 64 * 1024,
                }
            completed = runner(
                [
                    openusage_path(),
                    "export",
                    "--output",
                    "-",
                    "--format",
                    "json",
                    "--source",
                    source,
                ],
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=self._subprocess_environment(),
                **options,
            )
        except BoundedProcessError as error:
            reason = "timed out" if error.code == "timeout" else "output exceeded limit"
            raise OpenUsageExportError(reason) from None
        except subprocess.TimeoutExpired:
            raise OpenUsageExportError(f"timed out after {timeout}s") from None
        except OSError as error:
            raise OpenUsageExportError(
                f"could not start exporter ({type(error).__name__})"
            ) from None
        except Exception as error:
            raise OpenUsageExportError(
                f"export runner failed ({type(error).__name__})"
            ) from None

        if completed.returncode != 0:
            raise OpenUsageExportError(
                f"exporter exited with code {completed.returncode}"
            )
        try:
            payload = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError, UnicodeError):
            raise OpenUsageExportError("exporter returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise OpenUsageExportError("exporter returned an invalid envelope")
        snapshots = payload.get("snapshots")
        if not isinstance(snapshots, list) or not snapshots:
            raise OpenUsageExportError("exporter returned no snapshots")
        try:
            overview = self.parse(payload, self.clock())
        except Exception as error:
            raise OpenUsageExportError(
                f"snapshot parsing failed ({type(error).__name__})"
            ) from None
        if not overview.cards:
            raise OpenUsageExportError("exporter returned no usable snapshots")
        return overview

    def fetch(self) -> Overview:
        try:
            overview = self._export("auto")
        except OpenUsageExportError as auto_error:
            logger.warning("OpenUsage auto export failed: %s", auto_error)
            try:
                return self._export("direct")
            except OpenUsageExportError as direct_error:
                logger.warning("OpenUsage direct export failed: %s", direct_error)
                return Overview(
                    [self._error_card(f"auto: {auto_error}; direct: {direct_error}")]
                )

        cursor = next(
            (card for card in overview.cards if card.provider_id == "cursor"), None
        )
        if cursor is None or (
            cursor.status == ProviderStatus.OK and cursor.remaining_percent is not None
        ):
            return overview

        try:
            direct = self._export("direct")
        except OpenUsageExportError as error:
            logger.warning("OpenUsage direct Cursor enrichment failed: %s", error)
            return overview
        direct_cursor = next(
            (
                card
                for card in direct.cards
                if card.provider_id == "cursor"
                and card.status == ProviderStatus.OK
                and card.remaining_percent is not None
            ),
            None,
        )
        if direct_cursor is None:
            return overview
        return Overview(
            [
                direct_cursor if card.provider_id == "cursor" else card
                for card in overview.cards
            ]
        )

    def _error_card(self, error: str) -> ProviderCard:
        return ProviderCard(
            provider_id="openusage",
            name="OpenUsage",
            category=Category.LOCAL,
            status=ProviderStatus.ERROR,
            primary="OpenUsage refresh unavailable",
            detail=None,
            remaining_percent=None,
            resets_at=None,
            source="OpenUsage",
            refreshed_at=self.clock(),
            last_error=error,
            family_id="openusage",
            credential_source="openusage",
            source_kind="openusage",
        )
