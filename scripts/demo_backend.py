"""Clean demo backend for UsageHub UI verification.

Seeds an in-memory SQLite ledger with realistic 30-day activity, then starts:
- a TCP web-dashboard server on 127.0.0.1:17822 (for the Electron desktop shell)
- a Unix-domain local-API server at ~/.local/state/openusage-bar/openusage.sock (for the Swift app)

Usage:
    USAGEHUB_COLLECTOR=/path/to/scripts/demo_backend.py npm start --prefix desktop
    # or directly:
    python scripts/demo_backend.py dashboard --port 17822
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Allow running this script directly from the repo root or from scripts/.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from openusage_bar.activity_store import (
    ActivityStore,
    BalanceObservation,
    DailyCostRow,
    DailyUsageRow,
    ProviderInstance,
    QuotaObservation,
)
from openusage_bar.local_api import create_tcp_server, create_unix_server
from openusage_bar.query import QueryService
from openusage_bar.web_dashboard import make_dashboard_server

DEFAULT_PORT = 17822
DEFAULT_SOCKET = Path.home() / ".local" / "state" / "openusage-bar" / "openusage.sock"
DEMO_SOCKET = Path.home() / ".local" / "state" / "openusage-bar" / "openusage-demo.sock"


def _remove_owned_socket(path: Path) -> None:
    """Remove a stale socket only when it is a socket owned by this user."""
    import stat

    try:
        info = path.stat()
    except FileNotFoundError:
        return
    if info.st_uid != os.getuid():
        return
    if not stat.S_ISSOCK(info.st_mode):
        return
    path.unlink()
NOW = datetime.now(timezone.utc)
TODAY = date.today()

MODELS = [
    ("openai", "gpt-5.5"),
    ("openai", "o5-mini"),
    ("anthropic", "claude-opus-5"),
    ("anthropic", "claude-sonnet-5"),
    ("deepseek", "deepseek-chat-v3"),
    ("gemini_api", "gemini-2.5-pro"),
    ("minimax", "minimax-text-01"),
]

LOCAL_TOOLS = [
    ("hermes", "hermes-cli", "local_tool"),
    ("openclaw", "openclaw-local", "local_tool"),
    ("kiro_cli", "kiro-agent", "subscription"),
]

QUOTAS = [
    ("openai", "tier_1", "USD", "450.00", "500.00", "50.00", 0.10),
    ("anthropic", "build_tier", "USD", "820.00", "1000.00", "180.00", 0.18),
    ("minimax", "five_hour", "percent", "72", "100", "28", 0.28),
    ("gemini_api", "daily_free", "count", "42", "1500", "1458", 0.97),
]


def _seed(store: ActivityStore) -> None:
    """Populate the ledger with 30 days of synthetic activity."""
    for provider_id, model_id in MODELS:
        store.upsert_provider_instance(ProviderInstance(
            provider_id=f"{provider_id}-primary",
            family_id=provider_id,
            display_name=f"{provider_id.title()} primary",
            category="api" if provider_id != "minimax" else "subscription",
            credential_source="openusage",
            source_kind="openusage",
            observed_at=(NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        ))

    for provider_id, model_id, category in LOCAL_TOOLS:
        store.upsert_provider_instance(ProviderInstance(
            provider_id=provider_id,
            family_id=provider_id,
            display_name=f"{provider_id.title()} local",
            category=category,
            credential_source="openusage",
            source_kind="openusage",
            observed_at=(NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        ))

    for offset in range(30):
        day = (TODAY - timedelta(days=29 - offset)).isoformat()
        for provider_id, model_id in MODELS:
            base = 2000 + offset * 100 + hash(model_id) % 500
            input_tokens = base
            output_tokens = base // 2
            cache_read = base // 4 if offset % 3 == 0 else 0
            cache_creation = base // 8 if offset % 5 == 0 else 0
            reasoning = base // 3 if "o" in model_id or "reasoning" in model_id else None
            total = input_tokens + output_tokens + cache_read + cache_creation + (reasoning or 0)
            cost = f"{(total / 1_000_000 * 2.5):.4f}" if provider_id != "minimax" else None
            store.replace_daily_usage(
                f"{provider_id}-primary",
                day,
                [DailyUsageRow(
                    day=day,
                    provider_id=f"{provider_id}-primary",
                    model_id=model_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read,
                    cache_creation_tokens=cache_creation,
                    reasoning_tokens=reasoning,
                    total_tokens=total,
                    cost_amount=cost,
                    cost_currency="USD" if cost else None,
                    cost_basis="provider_reported" if cost else None,
                    quality="direct",
                    imported_at=f"{day}T09:00:00Z",
                )],
            )
            for lt_provider, lt_model, _category in LOCAL_TOOLS:
                lt_base = 500 + offset * 50 + hash(lt_model) % 200
                lt_input = lt_base
                lt_output = lt_base // 2
                lt_total = lt_input + lt_output
                store.replace_daily_usage(
                    lt_provider,
                    day,
                    [DailyUsageRow(
                        day=day,
                        provider_id=lt_provider,
                        model_id=lt_model,
                        input_tokens=lt_input,
                        output_tokens=lt_output,
                        cache_read_tokens=0,
                        cache_creation_tokens=0,
                        reasoning_tokens=None,
                        total_tokens=lt_total,
                        cost_amount=None,
                        cost_currency=None,
                        cost_basis=None,
                        quality="direct",
                        imported_at=f"{day}T09:00:00Z",
                    )],
                )
            if cost:
                store.replace_daily_costs(
                    f"{provider_id}-primary",
                    day,
                    [DailyCostRow(
                        day=day,
                        provider_id=f"{provider_id}-primary",
                        cost_kind="actual",
                        currency="USD",
                        amount=cost,
                        basis="provider_reported",
                        quality="direct",
                        imported_at=f"{day}T09:00:00Z",
                    )],
                )

    for provider_id, quota_name, unit, used, limit, remaining, ratio in QUOTAS:
        for offset in range(7):
            observed = (NOW - timedelta(days=6 - offset, hours=2)).isoformat().replace("+00:00", "Z")
            ratio_trend = max(0.0, ratio - offset * 0.02)
            store.record_quota(QuotaObservation(
                record_id=f"{provider_id}.{quota_name}.{offset}",
                observed_at=observed,
                provider_id=f"{provider_id}-primary",
                quota_name=quota_name,
                unit=unit,
                used=used,
                quota_limit=limit,
                remaining=remaining,
                remaining_ratio=ratio_trend,
                resets_at=(NOW + timedelta(hours=4)).isoformat().replace("+00:00", "Z"),
                period_start=None,
                period_end=None,
                state="ok" if ratio_trend > 0.15 else "warn",
                quality="direct",
                stale=False,
            ))
        store.record_source_success(f"{provider_id}-primary", "current.usage", NOW)
        store.record_source_success(f"{provider_id}-primary", "current.quota", NOW)
        # Demo balance for provider accounts.
        balance_map = {
            "openai": ("50.00", "USD"),
            "anthropic": ("180.00", "USD"),
            "minimax": ("28.00", "USD"),
            "gemini_api": ("1458.00", "USD"),
        }
        if provider_id in balance_map:
            amount, currency = balance_map[provider_id]
            store.record_balance(
                BalanceObservation(
                    record_id=f"{provider_id}-primary.balance",
                    observed_at=NOW.isoformat().replace("+00:00", "Z"),
                    provider_id=f"{provider_id}-primary",
                    currency=currency,
                    available=amount,
                    voucher=None,
                    cash=None,
                    state="ok",
                    quality="direct",
                    stale=False,
                )
            )
            store.record_source_success(f"{provider_id}-primary", "current.balance", NOW)
    for provider_id, _model_id, _category in LOCAL_TOOLS:
        store.record_source_success(provider_id, "openusage", NOW)


def _ensure_socket_dir() -> None:
    DEFAULT_SOCKET.parent.mkdir(parents=True, exist_ok=True)


def _start_background(server) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def main() -> None:
    parser = argparse.ArgumentParser(description="UsageHub demo backend")
    sub = parser.add_subparsers(dest="command")
    dashboard = sub.add_parser("dashboard", help="start dashboard-compatible server")
    dashboard.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    if args.command != "dashboard":
        parser.print_help()
        return

    store = ActivityStore(":memory:")
    _seed(store)
    query = QueryService(store, clock=lambda: NOW)

    web = make_dashboard_server(query, port=args.port, today=TODAY)
    web_thread = _start_background(web)
    print(f"UsageHub web dashboard on http://127.0.0.1:{args.port}")

    _ensure_socket_dir()
    _remove_owned_socket(DEMO_SOCKET)
    unix = create_unix_server(DEMO_SOCKET, query, clock=lambda: NOW)
    unix_thread = _start_background(unix)
    print(f"UsageHub local API on unix:{DEMO_SOCKET}")

    print("Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        web.shutdown()
        unix.shutdown()
        web.server_close()
        unix.server_close()


if __name__ == "__main__":
    main()
