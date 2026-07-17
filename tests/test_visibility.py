import json
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from openusage_bar.models import Category, Overview, ProviderCard, ProviderStatus
from openusage_bar.visibility import (
    ProviderVisibilityRow,
    ProviderVisibilityStore,
    hidden_ids_from_selection,
    visibility_rows,
    visible_overview,
)


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def card(
    provider_id: str,
    name: str | None = None,
    status: ProviderStatus = ProviderStatus.OK,
    category: Category = Category.LOCAL,
) -> ProviderCard:
    return ProviderCard(
        provider_id=provider_id,
        name=name or provider_id,
        category=category,
        status=status,
        primary="value",
        detail=None,
        remaining_percent=50 if category == Category.SUBSCRIPTION else None,
        resets_at=None,
        source="test",
        refreshed_at=NOW,
        stale=status == ProviderStatus.STALE,
    )


class ProviderVisibilityStoreTests(unittest.TestCase):
    def test_store_round_trips_sorted_hidden_ids_with_private_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "visibility.json"
            store = ProviderVisibilityStore(path)

            store.save({"openclaw", "claude_code"})

            self.assertEqual(store.load(), {"claude_code", "openclaw"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {
                    "version": 1,
                    "hidden_provider_ids": ["claude_code", "openclaw"],
                },
            )

    def test_missing_visibility_file_fails_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"

            self.assertEqual(ProviderVisibilityStore(path).load(), set())

    def test_malformed_or_invalid_visibility_file_fails_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "visibility.json"
            store = ProviderVisibilityStore(path)
            for payload in (
                "not-json",
                '{"version": 2, "hidden_provider_ids": []}',
                '{"version": 1, "hidden_provider_ids": ["bad id"]}',
                '{"version": 1, "hidden_provider_ids": ["kiro_cli", "kiro_cli"]}',
            ):
                path.write_text(payload, encoding="utf-8")
                self.assertEqual(store.load(), set())

    def test_save_rejects_invalid_provider_id_without_replacing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "visibility.json"
            store = ProviderVisibilityStore(path)
            store.save({"claude_code"})

            with self.assertRaises(ValueError):
                store.save({"bad id"})

            self.assertEqual(store.load(), {"claude_code"})


class ProviderVisibilityModelTests(unittest.TestCase):
    def test_visible_overview_excludes_hidden_without_mutating_complete_overview(self):
        complete = Overview([card("claude_code"), card("kiro_cli"), card("minimax")])

        visible = visible_overview(complete, {"claude_code"})

        self.assertEqual(
            [item.provider_id for item in visible.cards],
            ["kiro_cli", "minimax"],
        )
        self.assertEqual(
            [item.provider_id for item in complete.cards],
            ["claude_code", "kiro_cli", "minimax"],
        )

    def test_hidden_provider_is_removed_from_attention_and_title_input(self):
        complete = Overview(
            [
                card("claude_code", status=ProviderStatus.ERROR),
                card("minimax", category=Category.SUBSCRIPTION),
            ]
        )

        visible = visible_overview(complete, {"claude_code"})

        self.assertEqual(visible.title, "OU 50%")
        self.assertNotIn("claude_code", [item.provider_id for item in visible.cards])

    def test_visibility_rows_include_hidden_and_new_providers_default_visible(self):
        rows = visibility_rows(
            Overview([card("kiro_cli", "Kiro"), card("claude_code", "Claude Code")]),
            {"claude_code"},
        )

        self.assertEqual(
            [(row.provider_id, row.visible) for row in rows],
            [("claude_code", False), ("kiro_cli", True)],
        )

    def test_hidden_ids_from_selection_hides_unchecked_and_prunes_missing(self):
        rows = [
            ProviderVisibilityRow("claude_code", "Claude Code", True),
            ProviderVisibilityRow("kiro_cli", "Kiro", True),
        ]

        hidden = hidden_ids_from_selection(rows, {"kiro_cli", "removed_provider"})

        self.assertEqual(hidden, {"claude_code"})


if __name__ == "__main__":
    unittest.main()
