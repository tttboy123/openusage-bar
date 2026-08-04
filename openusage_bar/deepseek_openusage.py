"""DeepSeek balance and spend facts sourced from the OpenUsage export snapshot.

OpenUsage already polls DeepSeek's official balance endpoint with the user's
configured credentials. This read-only adapter turns that same snapshot into
balance and daily-cost ledger facts so OpenUsage Bar can display them without
ever receiving or storing the DeepSeek API key.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .activity_records import BalanceObservation, DailyCostRow
from .bounded_process import run_bounded
from .openusage_adapter import child_subprocess_environment, openusage_path
from .providers.contracts import (
    BalanceCollectionResult,
    BalanceFetchFailure,
    BalanceFetchSuccess,
    CostImportResult,
    CostImportSuccess,
    ImportFailure,
    SourceAttribution,
)


DEEPSEEK_PROVIDER_ID = "deepseek"
DEEPSEEK_BALANCE_SOURCE_ID = "openusage.deepseek.balance"
DEEPSEEK_COST_SOURCE_ID = "openusage.deepseek.cost"
DEEPSEEK_ACCOUNT_REF = "openusage"
MAX_EXPORT_BYTES = 8 * 1024 * 1024
EXPORT_TIMEOUT_SECONDS = 30.0
_EXPORT_CACHE_TTL_SECONDS = 30.0


def _decimal(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not decimal.is_finite() or decimal < 0:
        return None
    return format(decimal.quantize(Decimal("0.0001")).normalize(), "f")


def _timestamp(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class OpenUsageDeepSeekAdapter:
    """Publish DeepSeek balance and window spend from the OpenUsage snapshot."""

    source_id = DEEPSEEK_BALANCE_SOURCE_ID
    cost_source_id = DEEPSEEK_COST_SOURCE_ID
    source_priority = 20
    account_ref = DEEPSEEK_ACCOUNT_REF
    performance_source_class = "child_process"
    source_attribution = SourceAttribution(
        credential_source="openusage", source_kind="openusage"
    )

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        runner=None,
        environment: dict[str, str] | None = None,
        path_exists: Callable[[str], bool] | None = None,
    ) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.runner = runner
        self.environment = dict(environment if environment is not None else os.environ)
        self.path_exists = path_exists or os.path.isdir
        self._cached_payload: dict[str, Any] | None = None
        self._cached_at: float | None = None

    def _export_payload(self) -> dict[str, Any] | None:
        now = time.monotonic()
        if (
            self._cached_payload is not None
            and self._cached_at is not None
            and now - self._cached_at < _EXPORT_CACHE_TTL_SECONDS
        ):
            return self._cached_payload
        runner = self.runner or run_bounded
        options: dict[str, Any] = {}
        if self.runner is None:
            options = {
                "stdout_limit": MAX_EXPORT_BYTES,
                "stderr_limit": 128 * 1024,
            }
        try:
            completed = runner(
                [
                    openusage_path(),
                    "export",
                    "--output",
                    "-",
                    "--format",
                    "json",
                ],
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=EXPORT_TIMEOUT_SECONDS,
                env=child_subprocess_environment(self.environment, self.path_exists),
                **options,
            )
        except Exception:
            return None
        if completed.returncode != 0:
            return None
        try:
            payload = json.loads(completed.stdout)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("snapshots"), list):
            return None
        self._cached_payload = payload
        self._cached_at = now
        return payload

    def _snapshot(self) -> dict[str, Any] | None:
        payload = self._export_payload()
        if payload is None:
            return None
        for snapshot in payload["snapshots"]:
            if (
                isinstance(snapshot, dict)
                and snapshot.get("provider_id") == DEEPSEEK_PROVIDER_ID
            ):
                return snapshot
        return None

    def fetch_balance(self) -> BalanceCollectionResult:
        snapshot = self._snapshot()
        if snapshot is None:
            return BalanceCollectionResult(
                BalanceFetchFailure("source_unavailable"), self.source_attribution
            )
        metrics = snapshot.get("metrics")
        total = metrics.get("total_balance") if isinstance(metrics, dict) else None
        if not isinstance(total, dict):
            return BalanceCollectionResult(
                BalanceFetchFailure("invalid_export_v1"), self.source_attribution
            )
        available = _decimal(total.get("remaining"))
        currency = total.get("unit")
        if available is None or not isinstance(currency, str) or not currency:
            return BalanceCollectionResult(
                BalanceFetchFailure("invalid_export_v1"), self.source_attribution
            )
        observation = BalanceObservation(
            record_id=f"{DEEPSEEK_PROVIDER_ID}.openusage.balance",
            observed_at=_timestamp(snapshot.get("timestamp")),
            provider_id=DEEPSEEK_PROVIDER_ID,
            account_ref=self.account_ref,
            currency=currency.upper(),
            available=available,
            voucher=None,
            cash=None,
            state="ok" if snapshot.get("status") == "OK" else "unknown",
            quality="derived",
            stale=False,
            source_id=self.source_id,
        )
        return BalanceCollectionResult(
            BalanceFetchSuccess((observation,)), self.source_attribution
        )

    def fetch_costs(self, since: date, until: date) -> CostImportResult:
        if since > until:
            return ImportFailure("invalid_request")
        snapshot = self._snapshot()
        if snapshot is None:
            return ImportFailure("source_unavailable")
        metrics = snapshot.get("metrics")
        attributes = (
            snapshot.get("attributes")
            if isinstance(snapshot.get("attributes"), dict)
            else {}
        )
        if not isinstance(metrics, dict):
            return ImportFailure("invalid_export_v1")
        spend = metrics.get("window_credit_spend")
        if not isinstance(spend, dict):
            return ImportFailure("source_unavailable")
        used = _decimal(spend.get("used"))
        currency = spend.get("unit") or attributes.get("currency")
        if used is None or not isinstance(currency, str) or not currency:
            return ImportFailure("invalid_export_v1")
        today = self.clock().astimezone(timezone.utc).date()
        row = DailyCostRow(
            day=today.isoformat(),
            provider_id=DEEPSEEK_PROVIDER_ID,
            cost_kind="actual",
            currency=currency.upper(),
            amount=used,
            basis="openusage.window_credit_spend",
            quality="partial",
            imported_at=datetime.now(timezone.utc).isoformat(),
            account_ref=self.account_ref,
        )
        return CostImportSuccess(since, until, (row,))
