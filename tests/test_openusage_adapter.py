import json
import os
import subprocess
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from openusage_bar.models import Category, Overview, ProviderCard, ProviderStatus
from openusage_bar.openusage_adapter import OpenUsageAdapter
from openusage_bar.provider_catalog import catalog


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def envelope(*snapshots):
    return {"source": "test", "snapshots": list(snapshots)}


def snapshot(provider_id="kiro_cli", message="1 conversation, 1.3M tokens (est.)"):
    return {
        "provider_id": provider_id,
        "status": "OK",
        "message": message,
        "metrics": {},
    }


def cursor_snapshot(plan_metric=None, message="Pro — You've used 28% of your included usage"):
    metrics = {
        "plan_spend": {"used": 28},
        "plan_limit_usd": {"limit": 100},
        "billing_cycle_progress": {"remaining": 9},
    }
    if plan_metric is not None:
        metrics["plan_percent_used"] = plan_metric
    return {
        "provider_id": "cursor",
        "status": "OK",
        "message": message,
        "metrics": metrics,
    }


def completed(payload, returncode=0, stderr=""):
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def parsed_card(provider_id="kiro_cli"):
    return ProviderCard(
        provider_id=provider_id,
        name="Kiro",
        category=Category.SUBSCRIPTION,
        status=ProviderStatus.OK,
        primary="fresh",
        detail=None,
        remaining_percent=None,
        resets_at=None,
        source="OpenUsage",
        refreshed_at=NOW,
    )


class OpenUsageAdapterTests(unittest.TestCase):
    def test_default_export_runner_applies_stream_limits(self):
        result = completed(envelope(snapshot("codex")))
        with patch(
            "openusage_bar.openusage_adapter.run_bounded", return_value=result
        ) as bounded:
            overview = OpenUsageAdapter(clock=lambda: NOW).fetch()
        self.assertEqual(overview.cards[0].provider_id, "codex")
        self.assertEqual(bounded.call_args.kwargs["stdout_limit"], 16 * 1024 * 1024)
        self.assertEqual(bounded.call_args.kwargs["stderr_limit"], 64 * 1024)

    def test_snapshot_count_is_bounded(self):
        with self.assertRaises(ValueError):
            OpenUsageAdapter.parse(
                {"snapshots": [snapshot("codex")] * 4097}, NOW
            )

    def test_snapshot_field_counts_are_bounded(self):
        oversized = snapshot("codex")
        oversized["metrics"] = {f"metric_{index}": index for index in range(257)}
        with self.assertRaises(ValueError):
            OpenUsageAdapter.parse({"snapshots": [oversized]}, NOW)

    def test_cursor_cli_path_is_added_without_mutating_parent_environment(self):
        parent = {"PATH": "/usr/bin"}
        run = Mock(return_value=completed(envelope(snapshot())))
        adapter = OpenUsageAdapter(
            clock=lambda: NOW,
            runner=run,
            environment=parent,
            path_exists=lambda path: path
            == "/Applications/Cursor.app/Contents/Resources/app/bin",
        )

        adapter.fetch()

        child = run.call_args.kwargs["env"]
        self.assertEqual(parent, {"PATH": "/usr/bin"})
        self.assertEqual(
            child["PATH"].split(os.pathsep)[0],
            "/Applications/Cursor.app/Contents/Resources/app/bin",
        )

    def test_missing_cursor_app_keeps_original_path(self):
        run = Mock(return_value=completed(envelope(snapshot())))
        adapter = OpenUsageAdapter(
            clock=lambda: NOW,
            runner=run,
            environment={"PATH": "/usr/bin"},
            path_exists=lambda _path: False,
        )

        adapter.fetch()

        self.assertEqual(run.call_args.kwargs["env"]["PATH"], "/usr/bin")

    def test_headless_path_discovers_user_local_and_project_tool_bins(self):
        local = os.path.expanduser("~/.local/bin")
        project = os.path.expanduser("~/Documents/Codex/devtools/npm/bin")
        run = Mock(return_value=completed(envelope(snapshot())))
        adapter = OpenUsageAdapter(
            clock=lambda: NOW,
            runner=run,
            environment={"PATH": "/usr/bin", "SECRET_TOKEN": "must-not-pass"},
            path_exists=lambda path: path in {local, project},
        )

        adapter.fetch()

        child = run.call_args.kwargs["env"]
        parts = child["PATH"].split(os.pathsep)
        self.assertIn(local, parts)
        self.assertIn(project, parts)
        self.assertNotIn("SECRET_TOKEN", child)

    def test_child_environment_uses_minimal_allowlist_and_drops_credentials(self):
        parent = {
            "PATH": "/usr/bin",
            "HOME": "/Users/tester",
            "USER": "tester",
            "LOGNAME": "tester",
            "TMPDIR": "/tmp/tester",
            "LANG": "en_US.UTF-8",
            "LC_CTYPE": "UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "LC_API_KEY": "locale-shaped-secret",
            "LC_SECRET": "private-locale-secret",
            "TZ": "UTC",
            "XDG_CONFIG_HOME": "/tmp/config",
            "OPENAI_API_KEY": "openai-secret",
            "ANTHROPIC_API_KEY": "anthropic-secret",
            "OASIS_TOKEN": "oasis-secret",
            "COOKIE": "session=secret",
            "ARBITRARY_PRIVATE_VALUE": "private",
        }
        run = Mock(return_value=completed(envelope(snapshot())))
        adapter = OpenUsageAdapter(
            clock=lambda: NOW,
            runner=run,
            environment=parent,
            path_exists=lambda _path: False,
        )

        adapter.fetch()

        child = run.call_args.kwargs["env"]
        self.assertEqual(
            child,
            {
                "PATH": "/usr/bin",
                "HOME": "/Users/tester",
                "USER": "tester",
                "LOGNAME": "tester",
                "TMPDIR": "/tmp/tester",
                "LANG": "en_US.UTF-8",
                "LC_CTYPE": "UTF-8",
                "LC_ALL": "en_US.UTF-8",
                "TZ": "UTC",
                "XDG_CONFIG_HOME": "/tmp/config",
            },
        )
        self.assertEqual(parent["OPENAI_API_KEY"], "openai-secret")

    def test_cursor_maps_plan_quota_to_remaining_subscription(self):
        card = OpenUsageAdapter.parse(
            envelope(cursor_snapshot({"remaining": 72, "used": 28})), NOW
        ).cards[0]

        self.assertEqual(card.provider_id, "cursor")
        self.assertEqual(card.name, "Cursor")
        self.assertEqual(card.category, Category.SUBSCRIPTION)
        self.assertEqual(card.primary, "72% remaining")
        self.assertEqual(card.remaining_percent, 72.0)
        self.assertEqual(card.detail, "Pro plan · $28 / $100 spent")
        self.assertIsNone(card.resets_at)

    def test_cursor_remaining_wins_over_used_and_is_clamped(self):
        for metric, expected in (
            ({"remaining": 120, "used": 99}, 100.0),
            ({"remaining": -5, "used": 0}, 0.0),
        ):
            with self.subTest(metric=metric):
                card = OpenUsageAdapter.parse(
                    envelope(cursor_snapshot(metric)), NOW
                ).cards[0]
                self.assertEqual(card.remaining_percent, expected)

    def test_cursor_missing_remaining_uses_inverse_of_used(self):
        card = OpenUsageAdapter.parse(
            envelope(cursor_snapshot({"used": 28})), NOW
        ).cards[0]

        self.assertEqual(card.primary, "72% remaining")
        self.assertEqual(card.remaining_percent, 72.0)

    def test_cursor_never_uses_billing_cycle_progress_as_quota(self):
        original = "Pro — quota unavailable"
        card = OpenUsageAdapter.parse(
            envelope(cursor_snapshot(None, message=original)), NOW
        ).cards[0]

        self.assertEqual(card.primary, original)
        self.assertIsNone(card.remaining_percent)
        self.assertIsNone(card.detail)

    def test_unknown_provider_becomes_generic_card(self):
        payload = {
            "snapshots": [
                {
                    "provider_id": "future_ai",
                    "status": "OK",
                    "message": "12 credits",
                    "metrics": {},
                }
            ]
        }

        card = OpenUsageAdapter.parse(payload, NOW).cards[0]

        self.assertEqual(card.name, "Future Ai")
        self.assertEqual(card.primary, "12 credits")
        self.assertEqual(card.source, "OpenUsage")
        self.assertEqual(card.category, Category.API)
        self.assertEqual(card.family_id, "future_ai")

    def test_every_exact_upstream_id_keeps_its_own_family(self):
        payload = envelope(
            *(snapshot(provider_id, "usage") for provider_id in catalog.upstream_family_ids)
        )

        cards = OpenUsageAdapter.parse(payload, NOW).cards

        self.assertEqual(
            {card.provider_id: card.family_id for card in cards},
            {provider_id: provider_id for provider_id in catalog.upstream_family_ids},
        )

    def test_catalog_is_the_single_source_of_provider_names_and_categories(self):
        provider_ids = (
            "claude_code", "opencode", "kimi_cli", "gemini_cli", "qwen_cli",
            "zai", "moonshot",
        )

        cards = {
            card.provider_id: card
            for card in OpenUsageAdapter.parse(
                envelope(*(snapshot(provider_id, "usage") for provider_id in provider_ids)),
                NOW,
            ).cards
        }

        category = {
            "subscription": Category.SUBSCRIPTION,
            "local_tool": Category.LOCAL,
            "api": Category.API,
        }
        for provider_id in provider_ids:
            with self.subTest(provider_id=provider_id):
                family = catalog.require(provider_id)
                self.assertEqual(cards[provider_id].name, family.display_name)
                self.assertEqual(cards[provider_id].category, category[family.category])

    def test_invalid_or_missing_provider_ids_are_not_normalized_into_identity(self):
        cards = OpenUsageAdapter.parse(
            envelope(
                snapshot("bad provider", "usage"),
                snapshot(" codex ", "usage"),
                {"status": "OK", "message": "usage", "metrics": {}},
            ),
            NOW,
        ).cards

        self.assertEqual(cards, [])

    def test_raw_attributes_are_ignored_and_never_reach_card_or_logs(self):
        secret_values = {
            "email": "private@example.test",
            "path": "/Users/private/.config/provider",
            "organization_uuid": "d11f9ed8-1111-4222-8333-abcdeffedcba",
            "authorization": "Bearer sanitized-secret-value",
        }
        raw = snapshot("future_agent", "usage")
        raw["attributes"] = secret_values

        with self.assertNoLogs("openusage_bar.openusage_adapter", level="WARNING"):
            card = OpenUsageAdapter.parse(envelope(raw), NOW).cards[0]

        self.assertEqual(card.family_id, "future_agent")
        representation = repr(card)
        for value in secret_values.values():
            self.assertNotIn(value, representation)

    def test_maps_local_provider_and_metric_summary(self):
        payload = {
            "snapshots": [
                {
                    "provider_id": "openclaw",
                    "status": "OK",
                    "metrics": {"window_tokens": 44800, "window_cost": 0.0127},
                }
            ]
        }

        card = OpenUsageAdapter.parse(payload, NOW).cards[0]

        self.assertEqual(card.category, Category.LOCAL)
        self.assertEqual(card.primary, "44.8K tokens")
        self.assertEqual(card.detail, "$0.0127")

    def test_subscription_tools_and_local_runtimes_use_product_categories(self):
        payload = {
            "snapshots": [
                {"provider_id": "codex", "status": "OK", "message": "Codex usage"},
                {"provider_id": "kiro_cli", "status": "OK", "message": "Kiro usage"},
                {"provider_id": "hermes", "status": "OK", "message": "Hermes usage"},
                {"provider_id": "openclaw", "status": "OK", "message": "OpenClaw usage"},
            ]
        }

        cards = {card.provider_id: card for card in OpenUsageAdapter.parse(payload, NOW).cards}

        self.assertEqual(cards["codex"].category, Category.SUBSCRIPTION)
        self.assertEqual(cards["kiro_cli"].category, Category.SUBSCRIPTION)
        self.assertEqual(cards["hermes"].category, Category.LOCAL)
        self.assertEqual(cards["openclaw"].category, Category.LOCAL)

    def test_maps_openusage_metric_envelopes(self):
        payload = {
            "snapshots": [
                {
                    "provider_id": "openclaw",
                    "status": "OK",
                    "metrics": {
                        "window_tokens": {"used": 44845, "unit": "tokens", "window": "all-time"},
                        "window_cost": {"used": 0.012678288, "unit": "USD", "window": "all-time"},
                    },
                }
            ]
        }

        card = OpenUsageAdapter.parse(payload, NOW).cards[0]

        self.assertEqual(card.primary, "44.8K tokens")
        self.assertEqual(card.detail, "$0.0127")

    def test_auto_success_does_not_call_direct_and_never_uses_shell(self):
        run = Mock(return_value=completed(envelope(snapshot())))

        result = OpenUsageAdapter(clock=lambda: NOW, runner=run).fetch()

        self.assertEqual(result.cards[0].provider_id, "kiro_cli")
        self.assertEqual(run.call_count, 1)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")
        self.assertEqual(run.call_args.args[0][1:], ["export", "--output", "-", "--format", "json", "--source", "auto"])

    def test_incomplete_auto_cursor_is_replaced_by_direct_cursor_quota(self):
        auto_cursor = snapshot("cursor", None)
        auto_cursor["status"] = "UNKNOWN"
        run = Mock(
            side_effect=[
                completed(envelope(snapshot("kiro_cli"), auto_cursor)),
                completed(envelope(cursor_snapshot({"remaining": 100, "used": 0}))),
            ]
        )

        result = OpenUsageAdapter(clock=lambda: NOW, runner=run).fetch()

        cards = {card.provider_id: card for card in result.cards}
        self.assertEqual(cards["kiro_cli"].primary, "1 conversation, 1.3M tokens (est.)")
        self.assertEqual(cards["cursor"].primary, "100% remaining")
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ["auto", "direct"])

    def test_failed_direct_cursor_enrichment_keeps_auto_cursor(self):
        auto_cursor = snapshot("cursor", None)
        auto_cursor["status"] = "UNKNOWN"
        run = Mock(
            side_effect=[
                completed(envelope(auto_cursor)),
                completed({}, returncode=1),
            ]
        )

        result = OpenUsageAdapter(clock=lambda: NOW, runner=run).fetch()

        self.assertEqual(result.cards[0].provider_id, "cursor")
        self.assertEqual(result.cards[0].status, ProviderStatus.UNKNOWN)
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ["auto", "direct"])

    def test_empty_auto_falls_back_to_direct(self):
        run = Mock(
            side_effect=[
                completed(envelope()),
                completed(envelope(snapshot())),
            ]
        )

        result = OpenUsageAdapter(clock=lambda: NOW, runner=run).fetch()

        self.assertEqual(result.cards[0].provider_id, "kiro_cli")
        self.assertEqual(result.cards[0].status, ProviderStatus.OK)
        self.assertEqual(
            [call.args[0][-1] for call in run.call_args_list],
            ["auto", "direct"],
        )

    def test_nonzero_and_malformed_auto_results_fall_back_to_direct(self):
        for first in (
            completed({}, returncode=1, stderr="sensitive details"),
            completed("not-json"),
            completed([]),
        ):
            with self.subTest(first=first):
                run = Mock(
                    side_effect=[first, completed(envelope(snapshot()))]
                )

                result = OpenUsageAdapter(clock=lambda: NOW, runner=run).fetch()

                self.assertEqual(result.cards[0].provider_id, "kiro_cli")
                self.assertEqual(run.call_count, 2)

    def test_parse_failure_falls_back_to_direct(self):
        run = Mock(
            side_effect=[
                completed(envelope(snapshot("broken"))),
                completed(envelope(snapshot())),
            ]
        )
        with patch.object(
            OpenUsageAdapter,
            "parse",
            side_effect=[ValueError("secret payload"), Overview([parsed_card()])],
        ):
            result = OpenUsageAdapter(clock=lambda: NOW, runner=run).fetch()

        self.assertEqual(result.cards[0].provider_id, "kiro_cli")
        self.assertEqual(run.call_count, 2)

    def test_dual_timeout_becomes_one_sanitized_error_card(self):
        run = Mock(
            side_effect=subprocess.TimeoutExpired(
                cmd=["secret-command"], timeout=1, output="sensitive output"
            )
        )

        result = OpenUsageAdapter(clock=lambda: NOW, runner=run).fetch()

        self.assertEqual(len(result.cards), 1)
        card = result.cards[0]
        self.assertEqual(card.status, ProviderStatus.ERROR)
        self.assertEqual(card.provider_id, "openusage")
        self.assertEqual(card.primary, "OpenUsage refresh unavailable")
        self.assertNotIn("sensitive", card.last_error or "")
        self.assertNotIn("secret-command", card.last_error or "")
        self.assertEqual(
            [call.args[0][-1] for call in run.call_args_list],
            ["auto", "direct"],
        )


if __name__ == "__main__":
    unittest.main()
