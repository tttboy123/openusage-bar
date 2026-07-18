import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openusage_bar.codex_subscription import (
    CodexSubscriptionAdapter,
    latest_rate_limit_event,
    parse_rate_limit_card,
    parse_rate_limit_observations,
)
from openusage_bar.providers.contracts import QuotaFetchSuccess
from openusage_bar.models import Category, ProviderStatus


NOW = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)


def window(used_percent: float, minutes: int, resets_at: datetime) -> dict:
    return {
        "used_percent": used_percent,
        "window_minutes": minutes,
        "resets_at": int(resets_at.timestamp()),
    }


def rate_limits(primary: dict, secondary: dict | None = None) -> dict:
    return {
        "limit_id": "codex",
        "limit_name": None,
        "primary": primary,
        "secondary": secondary,
        "credits": None,
        "individual_limit": None,
        "plan_type": "prolite",
        "rate_limit_reached_type": None,
    }


class CodexSubscriptionTests(unittest.TestCase):
    def test_all_rate_limit_windows_become_scoped_quota_facts(self):
        five_reset = NOW + timedelta(hours=3)
        weekly_reset = NOW + timedelta(days=5)

        result = parse_rate_limit_observations(
            rate_limits(
                window(25, 300, five_reset),
                window(40, 10080, weekly_reset),
            ),
            NOW,
            NOW,
        )

        self.assertIsInstance(result, QuotaFetchSuccess)
        self.assertEqual(
            [observation.quota_window for observation in result.observations],
            ["five_hour", "weekly"],
        )
        self.assertEqual(
            [observation.remaining_ratio for observation in result.observations],
            [0.75, 0.6],
        )
        self.assertTrue(all(
            observation.source_id == "codex.local_rate_limits"
            and observation.applies_to_kind == "subscription"
            and observation.applies_to_model_ids == ()
            for observation in result.observations
        ))

    def test_dual_window_maps_to_minimax_style_subscription_card(self):
        reset_5h = NOW + timedelta(hours=2)
        reset_weekly = NOW + timedelta(days=5)

        card = parse_rate_limit_card(
            rate_limits(
                window(25, 300, reset_5h),
                window(40, 10080, reset_weekly),
            ),
            observed_at=NOW,
            now=NOW,
        )

        self.assertIsNotNone(card)
        self.assertEqual(card.provider_id, "codex")
        self.assertEqual(card.category, Category.SUBSCRIPTION)
        self.assertEqual(card.primary, "5h 75% remaining")
        self.assertEqual(card.detail, "Pro Lite · Weekly 60% remaining")
        self.assertEqual(card.remaining_percent, 75)
        self.assertEqual(card.resets_at, reset_5h)
        self.assertEqual(card.source, "Codex local rate limits")
        self.assertEqual(card.family_id, "codex")
        self.assertEqual(card.credential_source, "codex_local_log")
        self.assertEqual(card.source_kind, "local_log")
        self.assertEqual(card.status, ProviderStatus.OK)

    def test_weekly_only_snapshot_is_still_a_real_quota(self):
        reset_weekly = NOW + timedelta(days=5)

        card = parse_rate_limit_card(
            rate_limits(window(69, 10080, reset_weekly)),
            observed_at=NOW,
            now=NOW,
        )

        self.assertEqual(card.primary, "Weekly 31% remaining")
        self.assertEqual(card.detail, "Pro Lite plan")
        self.assertEqual(card.remaining_percent, 31)
        self.assertEqual(card.resets_at, reset_weekly)

    def test_expired_windows_are_not_presented_as_current_quota(self):
        card = parse_rate_limit_card(
            rate_limits(window(20, 300, NOW - timedelta(minutes=1))),
            observed_at=NOW - timedelta(hours=1),
            now=NOW,
        )

        self.assertIsNone(card)

    def test_zero_remaining_is_rate_limited_and_clamped(self):
        card = parse_rate_limit_card(
            rate_limits(window(120, 300, NOW + timedelta(hours=1))),
            observed_at=NOW,
            now=NOW,
        )

        self.assertEqual(card.remaining_percent, 0)
        self.assertEqual(card.status, ProviderStatus.RATE_LIMITED)
        self.assertEqual(card.primary, "5h limit reached")

    def test_latest_event_reads_only_rate_limit_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older = root / "older.jsonl"
            newer = root / "newer.jsonl"
            older.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-07-14T00:00:00Z",
                        "payload": {
                            "type": "token_count",
                            "rate_limits": rate_limits(
                                window(10, 10080, NOW + timedelta(days=2))
                            ),
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            newer.write_text(
                json.dumps({"type": "message", "content": "private conversation text"})
                + "\n"
                + json.dumps(
                    {
                        "timestamp": "2026-07-14T00:30:00Z",
                        "payload": {
                            "type": "token_count",
                            "rate_limits": rate_limits(
                                window(69, 10080, NOW + timedelta(days=5))
                            ),
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            event = latest_rate_limit_event(root, max_files=10)

        self.assertIsNotNone(event)
        self.assertEqual(event[0]["primary"]["used_percent"], 69)
        self.assertEqual(event[1], datetime(2026, 7, 14, 0, 30, tzinfo=timezone.utc))
        self.assertNotIn("private", repr(event))

    def test_adapter_returns_no_override_without_current_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = CodexSubscriptionAdapter(Path(directory), clock=lambda: NOW)

            overview = adapter.fetch()

        self.assertEqual(overview.cards, [])


if __name__ == "__main__":
    unittest.main()
