import json
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from openusage_bar.aggregator import (
    Aggregator, BoundedReadOnlyKeychain, CardCache, LedgerRefresher, merge_cards,
)
from openusage_bar.activity_records import ProviderInstance
from openusage_bar.daily_history import ActivityCollector, DailyImportResult
from openusage_bar.models import Category, Overview, ProviderCard, ProviderStatus
from openusage_bar.performance_timing import RefreshTimingRecorder
from openusage_bar.providers.contracts import (
    ProviderDescriptor,
    QuotaCollectionResult,
    QuotaFetchFailure,
    SourceAttribution,
)


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def card(
    provider_id="demo",
    status=ProviderStatus.OK,
    primary="73",
    source="test",
    *,
    family_id=None,
    remaining_percent=None,
    credential_source=None,
    source_kind=None,
):
    return ProviderCard(
        provider_id=provider_id,
        name="Demo",
        category=Category.API,
        status=status,
        primary=primary,
        detail=None,
        remaining_percent=remaining_percent,
        resets_at=None,
        source=source,
        refreshed_at=NOW,
        stale=False,
        last_error=None if status == ProviderStatus.OK else "failed",
        family_id=family_id or provider_id,
        credential_source=credential_source,
        source_kind=source_kind,
    )


class Adapter:
    def __init__(self, result):
        self.result = result

    def fetch(self):
        return self.result


class TimedAdapter(Adapter):
    performance_source_class = "network"


class BoundedReadOnlyKeychainTests(unittest.TestCase):
    def test_read_is_shell_free_bounded_and_returns_only_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "security-helper"
            helper.write_text(
                f"#!{sys.executable}\nimport sys\nsys.stdout.write('value\\n')\n",
                encoding="utf-8",
            )
            helper.chmod(0o700)
            keychain = BoundedReadOnlyKeychain(
                timeout_seconds=2, security_executable=str(helper)
            )
            with patch(
                "openusage_bar.keychain.subprocess.Popen", wraps=subprocess.Popen
            ) as popen:
                self.assertEqual(keychain.get("minimax-main"), "value")
        command = popen.call_args.args[0]
        options = popen.call_args.kwargs
        self.assertEqual(command[1:4], [
            "find-generic-password", "-s", "com.lune.openusage-menubar",
        ])
        self.assertEqual(command[-3:], ["-a", "minimax-main", "-w"])
        self.assertIs(options["stdin"], subprocess.DEVNULL)
        self.assertIs(options["stderr"], subprocess.DEVNULL)
        self.assertFalse(options["shell"])
        self.assertIs(options["stdout"], subprocess.PIPE)
        self.assertTrue(options["start_new_session"])

    def test_timeout_failure_oversize_and_write_return_no_secret_or_mutation(self):
        keychain = BoundedReadOnlyKeychain(timeout_seconds=1)
        self.assertIsNone(keychain.get("bad\naccount"))
        with self.assertRaises(RuntimeError):
            keychain.set("provider", "new-value")

    def test_real_overflow_is_bounded_and_process_group_is_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "security-helper"
            child_pid = Path(directory) / "child.pid"
            helper.write_text(
                f"#!{sys.executable}\n"
                "import os,sys,time\n"
                "child = os.fork()\n"
                "if child == 0:\n"
                "    time.sleep(30)\n"
                "    raise SystemExit\n"
                "open(sys.argv[5], 'w').write(str(child))\n"
                "sys.stdout.buffer.write(b'x' * 70000)\n"
                "sys.stdout.flush()\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            helper.chmod(0o700)
            keychain = BoundedReadOnlyKeychain(
                timeout_seconds=2, security_executable=str(helper)
            )

            self.assertIsNone(keychain.get(str(child_pid)))
            self.assertEqual(keychain.last_read_bytes, 65537)
            self.assertFalse(keychain.last_process_alive)
            pid = int(child_pid.read_text(encoding="utf-8"))
            for _ in range(20):
                state = subprocess.run(
                    ["/bin/ps", "-o", "stat=", "-p", str(pid)],
                    capture_output=True, text=True, check=False,
                ).stdout.strip()
                if not state:
                    break
                time.sleep(0.05)
            self.assertEqual(state, "")

    def test_real_silent_timeout_is_killed_and_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "security-helper"
            helper.write_text(
                f"#!{sys.executable}\nimport time\ntime.sleep(30)\n",
                encoding="utf-8",
            )
            helper.chmod(0o700)
            keychain = BoundedReadOnlyKeychain(
                timeout_seconds=1, security_executable=str(helper)
            )

            self.assertIsNone(keychain.get("provider"))
            self.assertFalse(keychain.last_process_alive)


class HeadlessRefresherFactoryTests(unittest.TestCase):
    def test_eager_local_usage_is_collected_before_slow_quota_refresh(self):
        events = []
        overview = Overview([])
        aggregator = Mock()
        aggregator.refresh.side_effect = lambda: events.append("quota") or overview
        collector = Mock()
        collector.refresh_usage.side_effect = lambda provider_ids: events.append(
            ("usage", provider_ids)
        ) or True
        collector.refresh.side_effect = lambda *_args, **_kwargs: events.append("ledger")

        LedgerRefresher(
            aggregator, collector, eager_usage_provider_ids=("codex",),
        ).refresh()

        self.assertEqual(events, [("usage", ("codex",)), "quota", "ledger"])

    def test_ledger_refresher_forwards_explicit_quota_results(self):
        overview = Overview([])
        aggregator = Mock()
        aggregator.refresh.return_value = overview
        collector = Mock()
        adapter = Mock()
        adapter.performance_source_class = "local_file"
        adapter.fetch.side_effect = AssertionError("card path must not run")
        failure = QuotaFetchFailure("quota_unavailable")
        adapter.fetch_quota.return_value = QuotaCollectionResult(
            result=failure,
            attribution=SourceAttribution(
                credential_source="codex_local_log",
                source_kind="local_log",
            ),
        )
        descriptor = ProviderDescriptor(
            provider_id="codex",
            family_id="codex",
            display_name="Codex",
            category="subscription",
        )

        LedgerRefresher(
            aggregator, collector,
            ((descriptor, "codex.local_rate_limits", adapter),),
            provider_descriptors=(descriptor,),
            clock=lambda: NOW,
        ).refresh()

        adapter.fetch_quota.assert_called_once_with()
        adapter.fetch.assert_not_called()
        collector.refresh.assert_called_once_with(
            overview,
            provider_instances=(ProviderInstance(
                provider_id="codex",
                family_id="codex",
                display_name="Codex",
                category="subscription",
                credential_source="codex_local_log",
                source_kind="local_log",
                observed_at="2026-07-14T00:00:00.000000Z",
            ),),
            provider_families={"codex": "codex"},
            balance_results=(),
            quota_results=((
                "codex", "codex.local_rate_limits", failure,
            ),),
        )

    def test_usage_only_provider_identity_does_not_require_a_card(self):
        overview = Overview([])
        aggregator = Mock()
        aggregator.refresh.return_value = overview
        collector = Mock()
        descriptor = ProviderDescriptor(
            provider_id="openai",
            family_id="openai",
            display_name="OpenAI Organization",
            category="api",
        )
        attribution = SourceAttribution(
            credential_source="openai_admin_api",
            source_kind="official_api",
        )

        LedgerRefresher(
            aggregator,
            collector,
            provider_descriptors=(descriptor,),
            provider_attributions=((descriptor, attribution),),
            clock=lambda: NOW,
        ).refresh()

        collector.refresh.assert_called_once_with(
            overview,
            provider_instances=(descriptor.observed(NOW, attribution),),
            provider_families={"openai": "openai"},
            balance_results=(),
            quota_results=(),
        )

    def test_daily_feed_uses_shared_bounded_keychain_and_no_redirect_client(self):
        from openusage_bar.aggregator import build_headless_refresher
        from openusage_bar.config import DailyUsageFeedConfig
        from openusage_bar.keychain import BoundedMacOSKeychain

        configured = DailyUsageFeedConfig(
            provider_id="glm-work", name="GLM Work", family_id="zai",
            endpoint="https://api.example.com/usage", method="GET",
            header_name="Authorization", auth_prefix="Bearer",
            items_path="data.items", date_path="day", model_path="model",
            input_tokens_path="input", output_tokens_path="output",
            total_tokens_path="total", since_parameter="from", until_parameter="to",
        )
        with patch(
            "openusage_bar.config.ProviderConfigStore.load", return_value=[configured]
        ):
            refresher = build_headless_refresher(Mock())

        importer = refresher.collector.official_importers["glm-work"]
        self.assertIsInstance(importer.keychain, BoundedMacOSKeychain)
        self.assertEqual(importer.client.allowed_redirect_hosts, frozenset())
        attribution = dict(
            (descriptor.provider_id, source)
            for descriptor, source in refresher.provider_attributions
        )["glm-work"]
        self.assertEqual(attribution.credential_source, "api_key")
        self.assertEqual(attribution.source_kind, "generic_https")

    def test_codex_local_sessions_are_primary_for_eager_collection(self):
        from openusage_bar.aggregator import build_headless_refresher
        from openusage_bar.codex_daily import CodexLocalDailyImporter

        with patch("openusage_bar.config.ProviderConfigStore.load", return_value=[]):
            refresher = build_headless_refresher(Mock())

        importer = refresher.collector.official_importers["codex"]
        self.assertIsInstance(importer, CodexLocalDailyImporter)
        self.assertEqual(importer.usage_source_id, "codex.local_sessions")
        self.assertEqual(refresher.eager_usage_provider_ids, ("codex",))

    def test_openusage_meta_source_is_not_treated_as_a_provider(self):
        from openusage_bar.aggregator import build_headless_refresher

        with patch("openusage_bar.config.ProviderConfigStore.load", return_value=[]):
            refresher = build_headless_refresher(Mock())

        self.assertNotIn(
            "openusage",
            {item.provider_id for item in refresher.provider_descriptors},
        )

    def test_minimax_reuses_keychain_and_client_for_quota_and_daily_tokens(self):
        from openusage_bar.aggregator import build_headless_refresher
        from openusage_bar.config import MiniMaxConfig
        from openusage_bar.minimax import (
            MiniMaxBillingImporter,
            MiniMaxCodingPlanAdapter,
        )

        with patch(
            "openusage_bar.config.ProviderConfigStore.load",
            return_value=[MiniMaxConfig("minimax-main", "MiniMax")],
        ):
            refresher = build_headless_refresher(Mock())

        quota_adapter = next(
            adapter
            for _descriptor, _source_id, adapter in refresher.quota_sources
            if isinstance(adapter, MiniMaxCodingPlanAdapter)
        )
        importer = refresher.collector.official_importers["minimax-main"]
        self.assertIsInstance(importer, MiniMaxBillingImporter)
        self.assertIs(importer.keychain, quota_adapter.keychain)
        self.assertIs(importer.client, quota_adapter.client)
        self.assertEqual(importer.client.allowed_redirect_hosts, frozenset())

    def test_openai_organization_uses_bounded_keychain_and_no_redirect_client(self):
        from openusage_bar.aggregator import build_headless_refresher
        from openusage_bar.config import OpenAIOrganizationConfig
        from openusage_bar.keychain import BoundedMacOSKeychain

        with patch(
            "openusage_bar.config.ProviderConfigStore.load",
            return_value=[OpenAIOrganizationConfig("openai", "OpenAI Org")],
        ):
            refresher = build_headless_refresher(Mock())

        importer = refresher.collector.official_importers["openai"]
        self.assertIsInstance(importer.keychain, BoundedMacOSKeychain)
        self.assertEqual(importer.client.allowed_redirect_hosts, frozenset())
        attribution = dict(
            (descriptor.provider_id, source)
            for descriptor, source in refresher.provider_attributions
        )["openai"]
        self.assertEqual(
            attribution.credential_source, "openai_admin_api"
        )
        self.assertEqual(attribution.source_kind, "official_api")

    def test_headless_sources_share_bounded_read_write_keychain(self):
        from openusage_bar.aggregator import build_headless_refresher
        from openusage_bar.config import StepPlanConfig
        from openusage_bar.keychain import BoundedMacOSKeychain
        from openusage_bar.step_plan import StepPlanAdapter

        with patch(
            "openusage_bar.config.ProviderConfigStore.load",
            return_value=[StepPlanConfig("step-plan-main", "Step Plan")],
        ):
            refresher = build_headless_refresher(Mock())

        step_plan = next(
            adapter
            for _descriptor, _source_id, adapter in refresher.quota_sources
            if isinstance(adapter, StepPlanAdapter)
        )
        self.assertIsInstance(step_plan.keychain, BoundedMacOSKeychain)


class AggregatorTests(unittest.TestCase):
    def test_stale_legacy_cache_cannot_erase_fresh_provider_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CardCache(Path(directory) / "cards.json")
            legacy = replace(
                card("hermes", primary="1 conversation", source="OpenUsage"),
                family_id=None, credential_source=None, source_kind=None,
            )
            cache.save([legacy])
            fresh = card(
                "hermes", status=ProviderStatus.UNKNOWN, primary=None,
                source="OpenUsage", family_id="hermes",
                credential_source="openusage", source_kind="openusage",
            )

            result = Aggregator([Adapter(Overview([fresh]))], cache, clock=lambda: NOW).refresh()

            self.assertEqual(result.cards[0].family_id, "hermes")
            self.assertEqual(result.cards[0].credential_source, "openusage")
            self.assertEqual(result.cards[0].source_kind, "openusage")

    def test_same_instance_cannot_be_rebound_to_another_family(self):
        with self.assertRaisesRegex(ValueError, "family"):
            merge_cards(
                [card("shared", family_id="future_agent")],
                [card("shared", family_id="minimax")],
            )

    def test_same_instance_override_inherits_missing_explicit_identity(self):
        base = card(
            "shared",
            family_id="future_agent",
            credential_source="openusage",
            source_kind="openusage",
        )
        legacy_override = replace(
            card("shared", primary="fresh", source="legacy"),
            family_id=None,
            credential_source=None,
            source_kind=None,
        )

        result = merge_cards([base], [legacy_override])[0]

        self.assertEqual(result.primary, "fresh")
        self.assertEqual(result.family_id, "future_agent")
        self.assertEqual(result.credential_source, "openusage")
        self.assertEqual(result.source_kind, "openusage")

    def test_enrichment_can_replace_only_same_instance_same_family(self):
        base = card(
            "minimax-1783978290",
            source="OpenUsage",
            family_id="future_agent",
        )
        enrichment = card(
            "minimax-1783978290",
            primary="55% remaining",
            source="MiniMax Coding Plan",
            family_id="minimax",
            remaining_percent=55,
        )

        with self.assertRaisesRegex(ValueError, "family"):
            merge_cards([base], [enrichment])

    def test_display_name_does_not_reclassify_generic_provider(self):
        generic = replace(
            card("minimax-foo", family_id="minimax-foo"),
            name="MiniMax Foo",
        )

        self.assertEqual(merge_cards([], [generic])[0].family_id, "minimax-foo")

    def test_direct_provider_wins_same_stable_id(self):
        self.assertEqual(
            merge_cards([card("minimax", source="OpenUsage")], [card("minimax", primary="55%", source="direct")]),
            [card("minimax", primary="55%", source="direct")],
        )

    def test_fresh_kiro_quota_overrides_openusage_consumption(self):
        result = merge_cards(
            [card("kiro_cli", primary="1 conversation", source="OpenUsage")],
            [
                card(
                    "kiro_cli",
                    primary="49.96 / 50 credits remaining",
                    source="Kiro subscription quota",
                )
            ],
        )

        self.assertEqual(result[0].source, "Kiro subscription quota")

    def test_empty_kiro_enrichment_leaves_openusage_consumption(self):
        result = merge_cards(
            [card("kiro_cli", primary="1 conversation", source="OpenUsage")], []
        )

        self.assertEqual(result[0].primary, "1 conversation")
        self.assertEqual(result[0].source, "OpenUsage")

    def test_failed_kiro_enrichment_keeps_last_good_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CardCache(Path(directory) / "cards.json")
            cache.save(
                [
                    card(
                        "kiro_cli",
                        primary="49 / 50 credits remaining",
                        source="Kiro subscription quota",
                        family_id="kiro_cli",
                        remaining_percent=98,
                        credential_source="kiro_codewhisperer_api",
                        source_kind="official_api",
                    )
                ]
            )

            result = Aggregator(
                [
                    Adapter(
                        card(
                            "kiro_cli",
                            primary="1 conversation",
                            source="OpenUsage",
                            family_id="kiro_cli",
                            credential_source="openusage",
                            source_kind="openusage",
                        )
                    ),
                    Adapter(Overview([])),
                ],
                cache,
            ).refresh()

            self.assertEqual(result.cards[0].remaining_percent, 98)
            self.assertTrue(result.cards[0].stale)
            self.assertEqual(result.cards[0].family_id, "kiro_cli")
            self.assertEqual(
                result.cards[0].credential_source, "kiro_codewhisperer_api"
            )
            self.assertEqual(result.cards[0].source_kind, "official_api")
            self.assertIn("1 conversation", result.cards[0].detail or "")

    def test_failed_codex_enrichment_keeps_last_good_quota_and_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CardCache(Path(directory) / "cards.json")
            cache.save(
                [
                    card(
                        "codex",
                        primary="5h 75% remaining",
                        source="Codex local rate limits",
                        family_id="codex",
                        remaining_percent=75,
                    )
                ]
            )

            result = Aggregator(
                [
                    Adapter(
                        card(
                            "codex",
                            primary="11.7M tokens",
                            source="OpenUsage",
                            family_id="codex",
                        )
                    ),
                    Adapter(Overview([])),
                ],
                cache,
            ).refresh()

            self.assertEqual(result.cards[0].remaining_percent, 75)
            self.assertTrue(result.cards[0].stale)
            self.assertIn("11.7M tokens", result.cards[0].detail or "")

    def test_failed_cursor_enrichment_keeps_last_good_quota_and_auto_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CardCache(Path(directory) / "cards.json")
            cache.save(
                [
                    card(
                        "cursor",
                        primary="72% remaining",
                        source="OpenUsage",
                        family_id="cursor",
                        remaining_percent=72,
                        credential_source="openusage",
                        source_kind="openusage",
                    )
                ]
            )

            result = Aggregator(
                [
                    Adapter(
                        card(
                            "cursor",
                            primary="1.3M tokens",
                            source="OpenUsage",
                            family_id="cursor",
                            credential_source="openusage",
                            source_kind="openusage",
                        )
                    )
                ],
                cache,
            ).refresh()

            cursor = result.cards[0]
            self.assertEqual(cursor.remaining_percent, 72)
            self.assertEqual(cursor.primary, "72% remaining")
            self.assertTrue(cursor.stale)
            self.assertEqual(cursor.credential_source, "openusage")
            self.assertEqual(cursor.source_kind, "openusage")
            self.assertIn("1.3M tokens", cursor.detail or "")
            self.assertEqual(
                cursor.last_error,
                "Quota enrichment did not return fresh data",
            )

    def _assert_legacy_quota_fallback_publishes_openusage_identity(
        self, provider_id, quota, remaining, quota_source, activity
    ):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cards.json"
            legacy = card(
                provider_id,
                primary=quota,
                source=quota_source,
                family_id=provider_id,
                remaining_percent=remaining,
            )
            raw = legacy.__dict__.copy()
            raw.pop("family_id")
            raw.pop("credential_source")
            raw.pop("source_kind")
            raw["category"] = raw["category"].value
            raw["status"] = raw["status"].value
            raw["refreshed_at"] = raw["refreshed_at"].isoformat()
            raw["resets_at"] = None
            path.write_text(json.dumps({"version": 1, "cards": [raw]}))
            current = card(
                provider_id,
                primary=activity,
                source="OpenUsage",
                family_id=provider_id,
                credential_source="openusage",
                source_kind="openusage",
            )

            overview = Aggregator(
                [Adapter(current), Adapter(Overview([]))], CardCache(path)
            ).refresh()

            result = overview.cards[0]
            self.assertEqual(result.remaining_percent, remaining)
            self.assertEqual(result.family_id, provider_id)
            self.assertEqual(result.credential_source, "openusage")
            self.assertEqual(result.source_kind, "openusage")
            self.assertIn(activity, result.detail or "")

            store = Mock()
            store.has_daily_history.return_value = True
            importer = Mock()
            importer.fetch.return_value = DailyImportResult(True, ())
            ActivityCollector(store, importer, clock=lambda: NOW).refresh(overview)

            instance = store.upsert_provider_instance.call_args.args[0]
            self.assertEqual(instance.provider_id, provider_id)
            self.assertEqual(instance.family_id, provider_id)
            self.assertEqual(instance.credential_source, "openusage")
            self.assertEqual(instance.source_kind, "openusage")

    def test_legacy_kiro_quota_fallback_publishes_openusage_identity(self):
        self._assert_legacy_quota_fallback_publishes_openusage_identity(
            "kiro_cli",
            "49 / 50 credits remaining",
            98,
            "Kiro subscription quota",
            "1 conversation",
        )

    def test_legacy_codex_quota_fallback_publishes_openusage_identity(self):
        self._assert_legacy_quota_fallback_publishes_openusage_identity(
            "codex",
            "5h 75% remaining",
            75,
            "Codex local rate limits",
            "11.7M tokens",
        )

    def test_failure_retains_last_success_and_marks_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CardCache(Path(directory) / "cards.json")
            cache.save([card()])
            result = Aggregator([Adapter(card(status=ProviderStatus.ERROR, primary=None))], cache, lambda: NOW + timedelta(minutes=6)).refresh()

            self.assertEqual(result.cards[0].primary, "73")
            self.assertTrue(result.cards[0].stale)
            self.assertEqual(result.cards[0].status, ProviderStatus.STALE)

    def test_fresh_rate_limited_quota_replaces_cached_success(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CardCache(Path(directory) / "cards.json")
            cache.save([card(primary="Plan connected")])
            exhausted = replace(
                card(status=ProviderStatus.RATE_LIMITED, primary="5h 0% remaining"),
                detail="Weekly 0% remaining",
                remaining_percent=0.0,
                last_error=None,
            )

            result = Aggregator([Adapter(exhausted)], cache).refresh()

            self.assertEqual(result.cards[0].primary, "5h 0% remaining")
            self.assertEqual(result.cards[0].remaining_percent, 0.0)
            self.assertFalse(result.cards[0].stale)
            self.assertEqual(result.cards[0].status, ProviderStatus.RATE_LIMITED)

    def test_shared_openusage_failure_retains_each_cached_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CardCache(Path(directory) / "cards.json")
            cache.save(
                [
                    card("kiro_cli", source="OpenUsage"),
                    card("hermes", source="OpenUsage"),
                ]
            )
            root_error = card(
                "openusage",
                status=ProviderStatus.ERROR,
                primary="OpenUsage refresh unavailable",
                source="OpenUsage",
            )

            result = Aggregator(
                [Adapter(root_error)],
                cache,
                lambda: NOW + timedelta(minutes=6),
            ).refresh()

            by_id = {item.provider_id: item for item in result.cards}
            self.assertEqual(set(by_id), {"hermes", "kiro_cli", "openusage"})
            self.assertTrue(by_id["kiro_cli"].stale)
            self.assertTrue(by_id["hermes"].stale)
            self.assertEqual(by_id["openusage"].status, ProviderStatus.ERROR)

    def test_fresh_openusage_result_clears_cached_root_error(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CardCache(Path(directory) / "cards.json")
            cache.save(
                [
                    card(
                        "openusage",
                        status=ProviderStatus.ERROR,
                        primary="OpenUsage refresh unavailable",
                        source="OpenUsage",
                    )
                ]
            )

            result = Aggregator(
                [Adapter(card("kiro_cli", source="OpenUsage"))],
                cache,
                lambda: NOW + timedelta(minutes=6),
            ).refresh()

            self.assertEqual(
                [item.provider_id for item in result.cards],
                ["kiro_cli"],
            )

    def test_cache_contains_display_data_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cards.json"
            CardCache(path).save([card()])
            text = path.read_text()
            self.assertNotIn("secret", text.lower())
            self.assertNotIn("endpoint", text.lower())
            self.assertEqual(json.loads(text)["cards"][0]["primary"], "73")

    def test_legacy_cache_is_migrated_to_product_category(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CardCache(Path(directory) / "cards.json")
            cache.save([replace(card("kiro_cli"), category=Category.LOCAL)])

            loaded = cache.load()

            self.assertEqual(loaded[0].category, Category.SUBSCRIPTION)

    def test_unbound_legacy_cache_does_not_conflict_with_explicit_family(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cards.json"
            cached = card("minimax-1783978290", family_id="minimax")
            raw = cached.__dict__.copy()
            raw.pop("family_id")
            raw.pop("credential_source")
            raw.pop("source_kind")
            raw["category"] = raw["category"].value
            raw["status"] = raw["status"].value
            raw["refreshed_at"] = raw["refreshed_at"].isoformat()
            raw["resets_at"] = None
            path.write_text(json.dumps({"version": 1, "cards": [raw]}))

            current = replace(
                cached,
                primary="55% remaining",
                remaining_percent=55,
                source="MiniMax Coding Plan",
            )
            result = Aggregator([Adapter(current)], CardCache(path)).refresh()

            self.assertEqual(result.cards[0].family_id, "minimax")
            self.assertEqual(result.cards[0].remaining_percent, 55)

    def test_refresh_records_only_adapter_source_class_timing(self):
        ticks = iter((3.0, 3.4))
        recorder = RefreshTimingRecorder(monotonic=lambda: next(ticks))
        adapter = TimedAdapter(card("private-provider"))

        with tempfile.TemporaryDirectory() as directory:
            Aggregator(
                [adapter],
                CardCache(Path(directory) / "cards.json"),
                timing_recorder=recorder,
            ).refresh()

        payload = recorder.snapshot()
        self.assertEqual(payload["classes"][0]["sourceClass"], "network")
        self.assertEqual(payload["classes"][0]["durationSecondsTotal"], 0.4)
        self.assertNotIn("private-provider", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
