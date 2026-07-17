import unittest
from datetime import datetime, timezone

from openusage_bar.models import Category, Overview, ProviderCard, ProviderStatus


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def make_card(provider_id, status=ProviderStatus.OK, remaining=None, stale=False):
    return ProviderCard(
        provider_id=provider_id,
        family_id=provider_id,
        name=provider_id,
        category=Category.SUBSCRIPTION,
        status=status,
        primary=None,
        detail=None,
        remaining_percent=remaining,
        resets_at=None,
        source="test",
        refreshed_at=NOW,
        stale=stale,
        last_error=None,
    )


class OverviewTitleTests(unittest.TestCase):
    def test_provider_card_keeps_explicit_family_identity(self):
        card = make_card("codex")

        self.assertEqual(card.provider_id, "codex")
        self.assertEqual(card.family_id, "codex")

    def test_title_prioritizes_attention(self):
        overview = Overview([make_card("a", ProviderStatus.AUTH), make_card("b", remaining=30)])
        self.assertEqual(overview.title, "OU ⚠ 1")

    def test_title_uses_lowest_fresh_subscription_remaining(self):
        overview = Overview([make_card("a", remaining=72.4), make_card("b", remaining=30.2, stale=True)])
        self.assertEqual(overview.title, "OU 72%")

    def test_title_falls_back_to_health(self):
        overview = Overview([make_card("a"), make_card("b", ProviderStatus.UNKNOWN)])
        self.assertEqual(overview.title, "OU 1/2")

    def test_empty_title(self):
        self.assertEqual(Overview([]).title, "OU --")


if __name__ == "__main__":
    unittest.main()
