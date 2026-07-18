#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openusage_bar.activity_store import (  # noqa: E402
    ActivityStore,
    DailyUsageRow,
    ProviderInstance,
    QuotaObservation,
)
from openusage_bar.query import QueryService, to_wire  # noqa: E402


NOW = datetime(2026, 7, 18, 1, 0, tzinfo=timezone.utc)
DAY = date(2026, 7, 18)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    args = parser.parse_args()
    store = ActivityStore(args.database)
    try:
        store.replace_daily_usage("codex", DAY.isoformat(), [DailyUsageRow(
            day=DAY.isoformat(), provider_id="codex", model_id="gpt-5.6-sol",
            input_tokens=30, output_tokens=10, cache_read_tokens=2,
            cache_creation_tokens=0, reasoning_tokens=None, total_tokens=42,
            cost_amount=None, cost_currency=None, cost_basis=None,
            quality="direct", imported_at="2026-07-18T00:30:00Z",
        )])
        store.record_quota(QuotaObservation(
            record_id="minimax.five_hour", observed_at="2026-07-18T00:00:00Z",
            provider_id="minimax", account_ref="primary", quota_name="Five hour",
            unit="percent", used="82", quota_limit="100", remaining="18",
            remaining_ratio=0.18, resets_at="2026-07-18T05:00:00Z",
            period_start="2026-07-18T00:00:00Z", period_end="2026-07-18T05:00:00Z",
            state="ok", quality="direct", stale=False,
        ))
        store.upsert_provider_instance(ProviderInstance(
            provider_id="minimax-primary", family_id="minimax",
            display_name="MiniMax Main", category="subscription",
            credential_source="minimax_builtin_api", source_kind="builtin_api",
            observed_at="2026-07-18T00:00:00Z",
        ))
        store.record_source_success("minimax", "current.quota", NOW)
        payload = to_wire(QueryService(store, clock=lambda: NOW).resource_snapshot(DAY))
    finally:
        store.close()
    args.expected.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
