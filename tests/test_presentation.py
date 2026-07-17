import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from openusage_bar.models import Category, Overview, ProviderCard, ProviderStatus
from openusage_bar.presentation import (
    QuotaSeverity,
    build_attention_summary,
    format_reset_detail,
    format_reset_label,
    humanize_refresh_age,
    present_row,
)


NOW = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)


def make_card(
    provider_id: str,
    category: Category,
    *,
    status: ProviderStatus = ProviderStatus.OK,
    primary: str | None = "60% remaining",
    detail: str | None = "Weekly quota",
    remaining_percent: float | None = None,
    resets_at: datetime | None = None,
    source: str = "test-source",
) -> ProviderCard:
    return ProviderCard(
        provider_id=provider_id,
        name=provider_id,
        category=category,
        status=status,
        primary=primary,
        detail=detail,
        remaining_percent=remaining_percent,
        resets_at=resets_at,
        source=source,
        refreshed_at=NOW,
    )


class CompactPresentationTests(unittest.TestCase):
    def test_attention_summary_prioritizes_auth_and_counts_stale_cards(self):
        stale = replace(make_card("cached", Category.LOCAL), stale=True)
        auth = replace(
            make_card("minimax", Category.SUBSCRIPTION),
            status=ProviderStatus.AUTH,
            primary=None,
            last_error="Key expired",
        )

        summary = build_attention_summary(Overview([stale, auth]))

        self.assertIsNotNone(summary)
        self.assertEqual(summary.provider_id, "minimax")
        self.assertEqual(summary.issue_count, 2)
        self.assertEqual(summary.message, "minimax authentication required")
        self.assertEqual(summary.status, ProviderStatus.AUTH)

    def test_attention_summary_is_absent_when_every_provider_is_healthy(self):
        summary = build_attention_summary(Overview([make_card("codex", Category.LOCAL)]))

        self.assertIsNone(summary)

    def test_openusage_refresh_failure_is_one_issue_for_all_cached_rows(self):
        root_error = make_card(
            "openusage",
            Category.LOCAL,
            status=ProviderStatus.ERROR,
            primary="OpenUsage refresh unavailable",
            source="OpenUsage",
        )
        kiro = replace(
            make_card("kiro_cli", Category.SUBSCRIPTION, source="OpenUsage"),
            stale=True,
        )
        hermes = replace(
            make_card("hermes", Category.LOCAL, source="OpenUsage"),
            stale=True,
        )

        summary = build_attention_summary(Overview([kiro, hermes, root_error]))

        self.assertIsNotNone(summary)
        self.assertEqual(summary.provider_id, "openusage")
        self.assertEqual(summary.issue_count, 1)
        self.assertEqual(summary.status, ProviderStatus.ERROR)

    def test_healthy_subscription_row_has_no_status_icon_and_has_quota(self):
        row = present_row(
            make_card("minimax", Category.SUBSCRIPTION, remaining_percent=60)
        )

        self.assertIsNone(row.status_icon)
        self.assertIsNone(row.status_text)
        self.assertEqual(row.quota_fraction, 0.6)
        self.assertEqual(row.quota_severity, QuotaSeverity.NORMAL)

    def test_error_row_uses_text_and_icon_not_color_alone(self):
        row = present_row(
            replace(
                make_card("minimax", Category.SUBSCRIPTION, primary=None),
                status=ProviderStatus.AUTH,
                last_error="Key expired",
            )
        )

        self.assertEqual(row.primary, "Auth required")
        self.assertEqual(row.status_icon, "exclamationmark.lock.fill")
        self.assertEqual(row.status_text, "Authentication required")
        self.assertIn("Key expired", row.expanded_detail)
        self.assertEqual(row.source_label, "Source: test-source")

    def test_stale_row_exposes_cached_state_even_with_ok_status(self):
        row = present_row(replace(make_card("codex", Category.LOCAL), stale=True))

        self.assertEqual(row.status_icon, "exclamationmark.triangle.fill")
        self.assertEqual(row.status_text, "Cached data")
        self.assertIn("Cached data", row.expanded_detail)

    def test_humanize_refresh_age_uses_compact_boundaries(self):
        self.assertEqual(humanize_refresh_age(NOW, NOW), "Updated just now")
        self.assertEqual(
            humanize_refresh_age(NOW - timedelta(minutes=3), NOW),
            "Updated 3m ago",
        )
        self.assertEqual(
            humanize_refresh_age(NOW - timedelta(hours=2), NOW),
            "Updated 2h ago",
        )
        self.assertEqual(
            humanize_refresh_age(NOW - timedelta(days=2), NOW),
            "Updated 2d ago",
        )
        self.assertEqual(
            humanize_refresh_age(NOW + timedelta(minutes=1), NOW),
            "Updated just now",
        )
        self.assertEqual(humanize_refresh_age(None, NOW), "Loading…")

    def test_reset_label_is_short_but_detail_is_exact(self):
        same_day = NOW.replace(hour=18, minute=30)
        same_year = datetime(2026, 7, 20, 9, 5, tzinfo=timezone.utc)
        next_year = datetime(2027, 1, 2, 9, 5, tzinfo=timezone.utc)

        self.assertEqual(format_reset_label(same_day, NOW), "18:30")
        self.assertEqual(format_reset_label(same_year, NOW), "Jul 20")
        self.assertEqual(format_reset_label(next_year, NOW), "2027-01-02")
        self.assertEqual(
            format_reset_detail(same_day),
            "Resets Jul 14, 2026 at 18:30",
        )
        self.assertIsNone(format_reset_label(None, NOW))

    def test_quota_fraction_is_clamped_and_only_for_subscriptions(self):
        empty = present_row(
            make_card("empty", Category.SUBSCRIPTION, remaining_percent=-2)
        )
        over = present_row(
            make_card("over", Category.SUBSCRIPTION, remaining_percent=120)
        )
        api = present_row(make_card("api", Category.API, remaining_percent=75))

        self.assertEqual(empty.quota_fraction, 0.0)
        self.assertEqual(empty.quota_severity, QuotaSeverity.CRITICAL)
        self.assertEqual(over.quota_fraction, 1.0)
        self.assertEqual(over.quota_severity, QuotaSeverity.NORMAL)
        self.assertIsNone(api.quota_fraction)
        self.assertIsNone(api.quota_severity)

    def test_quota_severity_boundaries_are_stable(self):
        normal = present_row(
            make_card("normal", Category.SUBSCRIPTION, remaining_percent=25)
        )
        low = present_row(
            make_card("low", Category.SUBSCRIPTION, remaining_percent=10)
        )
        critical = present_row(
            make_card("critical", Category.SUBSCRIPTION, remaining_percent=9.9)
        )

        self.assertEqual(normal.quota_severity, QuotaSeverity.NORMAL)
        self.assertEqual(low.quota_severity, QuotaSeverity.LOW)
        self.assertEqual(critical.quota_severity, QuotaSeverity.CRITICAL)


if __name__ == "__main__":
    unittest.main()
