from __future__ import annotations

import re
from datetime import datetime, timezone

from ..activity_store import QuotaObservation


def percent_observation(
    *,
    provider_id: str,
    source_id: str,
    quota_name: str,
    quota_window: str,
    remaining_percent: float,
    resets_at: datetime | None,
    observed_at: datetime,
    applies_to_kind: str = "subscription",
    applies_to_model_ids: tuple[str, ...] = (),
) -> QuotaObservation:
    remaining = min(100.0, max(0.0, float(remaining_percent)))
    reset = None if resets_at is None else resets_at.astimezone(timezone.utc).isoformat()
    quota_key = (
        re.sub(r"[^a-z0-9._-]+", "_", quota_name.casefold()).strip("_")
        or "quota"
    )
    return QuotaObservation(
        record_id=f"{provider_id}.{source_id}.{quota_window}.{quota_key}",
        provider_id=provider_id,
        account_ref="",
        source_id=source_id,
        quota_name=quota_name,
        quota_window=quota_window,
        applies_to_kind=applies_to_kind,
        applies_to_model_ids=applies_to_model_ids,
        unit="percent",
        used=str(100.0 - remaining),
        quota_limit="100",
        remaining=str(remaining),
        remaining_ratio=remaining / 100.0,
        resets_at=reset,
        period_start=None,
        period_end=None,
        observed_at=observed_at.astimezone(timezone.utc).isoformat(),
        state="ok" if remaining > 0 else "rate_limited",
        quality="direct",
        stale=False,
    )
