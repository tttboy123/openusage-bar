from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from openusage_bar.activity_store import (
    ActivityStore,
    DailyCostRow,
    DailyUsageRow,
    ProviderInstance,
)
from openusage_bar.daily_history import (
    ActivityCollector,
    DailyImportResult,
    OpenUsageDailyImporter,
)
from openusage_bar.codex_attribution import CodexAttributionResolver
from openusage_bar.models import Category, Overview, ProviderCard, ProviderStatus
from openusage_bar.openusage_adapter import CURSOR_CLI_DIRECTORIES, OpenUsageAdapter
from openusage_bar.openai_organization import (
    CostImportSuccess,
    ImportFailure,
    UsageImportSuccess,
)


NOW = datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc)
OLDER = datetime(2026, 7, 14, 1, 15, tzinfo=timezone.utc)
SINCE = date(2026, 7, 1)
UNTIL = date(2026, 7, 3)
FIXTURES = Path(__file__).parent / "fixtures"


def model_row(
    *,
    day: str = "2026-07-02",
    provider_id: str = "codex",
    model_id: str = "gpt-5.5",
    total_tokens: int = 11_735_817,
) -> DailyUsageRow:
    return DailyUsageRow(
        day=day,
        provider_id=provider_id,
        model_id=model_id,
        input_tokens=5_000_000,
        output_tokens=1_500_000,
        cache_read_tokens=5_000_000,
        cache_creation_tokens=235_817,
        reasoning_tokens=None,
        total_tokens=total_tokens,
        cost_amount=None,
        cost_currency=None,
        cost_basis=None,
        quality="derived",
        imported_at="2026-07-14T02:00:00Z",
    )


def cost_row(*, day: str = "2026-07-14", provider_id: str = "openai") -> DailyCostRow:
    return DailyCostRow(
        day=day,
        provider_id=provider_id,
        cost_kind="actual",
        currency="USD",
        amount="1.25",
        basis="provider_reported",
        quality="direct",
        imported_at="2026-07-14T02:00:00Z",
    )


def daily_payload(*rows: dict) -> dict:
    return {"kind": "daily", "rows": list(rows), "totals": {}}


def day_payload(day: str = "2026-07-02", *, quality: str | None = None) -> dict:
    model = {
        "key": "gpt-5.5",
        "label": "GPT 5.5",
        "input_tokens": 5_000_000,
        "output_tokens": 1_500_000,
        "cache_read_tokens": 5_000_000,
        "cache_creation_tokens": 235_817,
        "reasoning_tokens": None,
        "total_tokens": 11_735_817,
        "cost_usd": None,
    }
    if quality is not None:
        model["quality"] = quality
    return {
        "key": day,
        "label": day,
        "input_tokens": 5_000_000,
        "output_tokens": 1_500_000,
        "cache_read_tokens": 5_000_000,
        "cache_creation_tokens": 235_817,
        "reasoning_tokens": 0,
        "total_tokens": 11_735_817,
        "cost_usd": 0,
        "models": ["gpt-5.5"],
        "model_breakdown": [model],
    }


def completed(payload: object, *, returncode: int = 0, stderr: str = ""):
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def card(
    provider_id: str,
    *,
    category: Category = Category.SUBSCRIPTION,
    status: ProviderStatus = ProviderStatus.OK,
    remaining_percent: float | None = None,
    stale: bool = False,
    last_error: str | None = None,
    refreshed_at: datetime = NOW,
    family_id: str | None = None,
    credential_source: str = "openusage",
    source_kind: str = "openusage",
) -> ProviderCard:
    return ProviderCard(
        provider_id=provider_id,
        name=provider_id.title(),
        category=category,
        status=status,
        primary=None,
        detail=None,
        remaining_percent=remaining_percent,
        resets_at=datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc),
        source="test",
        refreshed_at=refreshed_at,
        stale=stale,
        last_error=last_error,
        family_id=family_id or provider_id,
        credential_source=credential_source,
        source_kind=source_kind,
    )


class OpenUsageDailyImporterTests(unittest.TestCase):
    @staticmethod
    def _write_codex_session(root: Path, name: str, events: list[dict]) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _token_event(timestamp: str) -> dict:
        return {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": {}}},
        }

    @staticmethod
    def _model_event(timestamp: str, model: str) -> dict:
        return {
            "timestamp": timestamp,
            "type": "turn_context",
            "payload": {"model": model},
        }

    def test_codex_unknown_moves_only_with_single_model_session_evidence(self):
        unknown_day = day_payload()
        unknown_day["model_breakdown"] = [
            {
                "key": "(unknown)", "input_tokens": 1, "output_tokens": 2,
                "cache_read_tokens": 3, "cache_creation_tokens": 4,
                "reasoning_tokens": None, "total_tokens": 10, "cost_usd": None,
            },
            {
                "key": "gpt-5.6-sol", "input_tokens": 10, "output_tokens": 20,
                "cache_read_tokens": 30, "cache_creation_tokens": 40,
                "reasoning_tokens": None, "total_tokens": 100, "cost_usd": None,
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_codex_session(root, "single.jsonl", [
                self._token_event("2026-07-02T00:01:00Z"),
                self._model_event("2026-07-02T00:02:00Z", "gpt-5.6-sol"),
                self._model_event("2026-07-02T01:00:00Z", "gpt-5.6-sol"),
            ])
            importer = OpenUsageDailyImporter(
                runner=Mock(return_value=completed(daily_payload(unknown_day))),
                clock=lambda: NOW,
                codex_attribution=CodexAttributionResolver(
                    sessions_root=root, local_timezone=timezone.utc
                ),
            )

            result = importer.fetch("codex", SINCE, UNTIL)

        self.assertTrue(result.ok)
        self.assertEqual([(row.model_id, row.total_tokens) for row in result.rows], [
            ("gpt-5.6-sol", 110),
        ])
        repaired = result.rows[0]
        self.assertEqual(
            (
                repaired.input_tokens,
                repaired.output_tokens,
                repaired.cache_read_tokens,
                repaired.cache_creation_tokens,
            ),
            (11, 22, 33, 44),
        )
        self.assertEqual(sum(row.total_tokens for row in result.rows), 110)

    def test_reused_openusage_local_client_slices_import_model_activity(self):
        providers = {
            "claude_code": "claude-sonnet-4.6",
            "opencode": "anthropic/claude-sonnet-4.6",
            "kimi_cli": "kimi-k2.5",
            "gemini_cli": "gemini-3-pro",
            "qwen_cli": "qwen3-coder",
        }
        for provider_id, model_id in providers.items():
            with self.subTest(provider_id=provider_id):
                payload = day_payload()
                payload["model_breakdown"][0]["key"] = model_id
                runner = Mock(return_value=completed(daily_payload(payload)))
                importer = OpenUsageDailyImporter(runner=runner, clock=lambda: NOW)

                result = importer.fetch(provider_id, SINCE, UNTIL)

                self.assertTrue(result.ok)
                self.assertEqual(len(result.rows), 1)
                self.assertEqual(result.rows[0].provider_id, provider_id)
                self.assertGreater(result.rows[0].total_tokens, 0)
                command = runner.call_args.args[0]
                self.assertEqual(command[command.index("--provider") + 1], provider_id)

    def test_reused_openusage_local_client_slices_preserve_no_data(self):
        for provider_id in (
            "claude_code", "opencode", "kimi_cli", "gemini_cli", "qwen_cli"
        ):
            with self.subTest(provider_id=provider_id):
                importer = OpenUsageDailyImporter(
                    runner=Mock(return_value=completed(daily_payload())),
                    clock=lambda: NOW,
                )

                result = importer.fetch(provider_id, SINCE, UNTIL)

                self.assertTrue(result.ok)
                self.assertEqual(result.rows, ())

    def test_codex_unknown_is_not_guessed_for_model_switch_or_missing_evidence(self):
        payload = daily_payload({
            "key": "2026-07-02",
            "model_breakdown": [{
                "key": "(unknown)", "input_tokens": 1, "output_tokens": 2,
                "cache_read_tokens": 3, "cache_creation_tokens": 4,
                "reasoning_tokens": None, "total_tokens": 10, "cost_usd": None,
            }],
        })
        cases = {
            "switch": [
                self._token_event("2026-07-02T00:01:00Z"),
                self._model_event("2026-07-02T00:02:00Z", "gpt-5.6-sol"),
                self._model_event("2026-07-02T00:03:00Z", "gpt-5.6-terra"),
            ],
            "missing": [self._token_event("2026-07-02T00:01:00Z")],
            "already-attributed": [
                self._model_event("2026-07-02T00:01:00Z", "gpt-5.6-sol"),
                self._token_event("2026-07-02T00:02:00Z"),
            ],
        }
        for name, events in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._write_codex_session(root, "case.jsonl", events)
                result = OpenUsageDailyImporter(
                    runner=Mock(return_value=completed(payload)),
                    clock=lambda: NOW,
                    codex_attribution=CodexAttributionResolver(
                        sessions_root=root, local_timezone=timezone.utc
                    ),
                ).fetch("codex", SINCE, UNTIL)

                self.assertTrue(result.ok)
                self.assertEqual([row.model_id for row in result.rows], ["unknown"])

    def test_codex_unknown_is_not_guessed_when_sessions_disagree(self):
        payload = daily_payload({
            "key": "2026-07-02",
            "model_breakdown": [{
                "key": "unknown", "input_tokens": 1, "output_tokens": 2,
                "cache_read_tokens": 3, "cache_creation_tokens": 4,
                "reasoning_tokens": None, "total_tokens": 10, "cost_usd": None,
            }],
        })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, model in (("sol.jsonl", "gpt-5.6-sol"), ("terra.jsonl", "gpt-5.6-terra")):
                self._write_codex_session(root, name, [
                    self._token_event("2026-07-02T00:01:00Z"),
                    self._model_event("2026-07-02T00:02:00Z", model),
                ])
            result = OpenUsageDailyImporter(
                runner=Mock(return_value=completed(payload)),
                clock=lambda: NOW,
                codex_attribution=CodexAttributionResolver(
                    sessions_root=root, local_timezone=timezone.utc
                ),
            ).fetch("codex", SINCE, UNTIL)

        self.assertEqual([row.model_id for row in result.rows], ["unknown"])

    def test_default_daily_runner_applies_stream_limits(self):
        with patch(
            "openusage_bar.daily_history.run_bounded",
            return_value=completed(daily_payload()),
        ) as bounded:
            result = OpenUsageDailyImporter(openusage_path="/opt/bin/openusage").fetch(
                "codex", SINCE, UNTIL
            )
        self.assertTrue(result.ok)
        self.assertEqual(bounded.call_args.kwargs["stdout_limit"], 16 * 1024 * 1024)
        self.assertEqual(bounded.call_args.kwargs["stderr_limit"], 64 * 1024)

    def test_daily_row_count_is_bounded(self):
        payload = {"kind": "daily", "rows": [{}] * 5001}
        result = OpenUsageDailyImporter(
            runner=Mock(return_value=completed(payload))
        ).fetch("codex", SINCE, UNTIL)
        self.assertEqual(result.error_code, "invalid_payload")

    def test_daily_model_label_length_is_bounded(self):
        day = day_payload()
        day["model_breakdown"][0]["key"] = "x" * 4097
        payload = daily_payload(day)
        result = OpenUsageDailyImporter(
            runner=Mock(return_value=completed(payload))
        ).fetch("codex", SINCE, UNTIL)
        self.assertEqual(result.error_code, "invalid_payload")

    def test_exact_shell_free_command_and_shared_safe_environment(self):
        parent = {"PATH": "/usr/bin", "SAFE": "yes"}
        run = Mock(return_value=completed(daily_payload()))
        importer = OpenUsageDailyImporter(
            openusage_path="/opt/bin/openusage",
            runner=run,
            environment=parent,
            path_exists=lambda path: path == CURSOR_CLI_DIRECTORIES[1],
        )

        result = importer.fetch("codex", SINCE, UNTIL)

        self.assertTrue(result.ok)
        self.assertEqual(parent, {"PATH": "/usr/bin", "SAFE": "yes"})
        self.assertEqual(
            run.call_args.args[0],
            [
                "/opt/bin/openusage",
                "daily",
                "--json",
                "--breakdown",
                "--offline",
                "--provider",
                "codex",
                "--since",
                "2026-07-01",
                "--until",
                "2026-07-03",
            ],
        )
        options = run.call_args.kwargs
        self.assertFalse(options["shell"])
        self.assertIs(options["stdin"], subprocess.DEVNULL)
        self.assertIs(options["stdout"], subprocess.PIPE)
        self.assertIs(options["stderr"], subprocess.PIPE)
        self.assertEqual(options["encoding"], "utf-8")
        self.assertEqual(options["errors"], "replace")
        self.assertEqual(options["timeout"], 30)
        self.assertEqual(options["env"]["PATH"].split(os.pathsep)[0], CURSOR_CLI_DIRECTORIES[1])

    def test_requires_daily_kind_and_accepts_valid_empty_payload(self):
        invalid = OpenUsageDailyImporter(
            runner=Mock(return_value=completed({"kind": "weekly", "rows": []}))
        ).fetch("codex", SINCE, UNTIL)
        empty = OpenUsageDailyImporter(
            runner=Mock(return_value=completed(daily_payload()))
        ).fetch("codex", SINCE, UNTIL)

        self.assertEqual(invalid, DailyImportResult(False, (), "invalid_envelope"))
        self.assertEqual(empty, DailyImportResult(True, ()))

    def test_parses_openusage_023_breakdown_without_double_counting(self):
        payload = daily_payload(
            day_payload("2026-06-30"),
            day_payload("2026-07-02"),
            day_payload("2026-07-04"),
        )
        result = OpenUsageDailyImporter(
            runner=Mock(return_value=completed(payload)), clock=lambda: NOW,
            codex_attribution=CodexAttributionResolver(
                sessions_root=FIXTURES / "no-codex-sessions"
            ),
        ).fetch("codex", SINCE, UNTIL)

        self.assertTrue(result.ok)
        self.assertEqual(len(result.rows), 1)
        row = result.rows[0]
        self.assertEqual((row.day, row.model_id), ("2026-07-02", "gpt-5.5"))
        self.assertEqual(row.total_tokens, 11_735_817)
        self.assertEqual(
            (
                row.input_tokens,
                row.output_tokens,
                row.cache_read_tokens,
                row.cache_creation_tokens,
            ),
            (5_000_000, 1_500_000, 5_000_000, 235_817),
        )
        self.assertIsNone(row.reasoning_tokens)
        self.assertIsNone(row.cost_amount)
        self.assertIsNone(row.cost_currency)
        self.assertIsNone(row.cost_basis)
        self.assertEqual(row.quality, "derived")

    def test_captured_openusage_023_fixture_canonicalizes_and_merges_unknown(self):
        payload = json.loads(
            (FIXTURES / "openusage_daily_023_unknown.json").read_text(
                encoding="utf-8"
            )
        )

        result = OpenUsageDailyImporter(
            runner=Mock(return_value=completed(payload)), clock=lambda: NOW,
            codex_attribution=CodexAttributionResolver(
                sessions_root=FIXTURES / "no-codex-sessions"
            ),
        ).fetch("codex", SINCE, UNTIL)

        self.assertTrue(result.ok)
        rows = {row.model_id: row for row in result.rows}
        self.assertEqual(set(rows), {"gpt-5.5", "o3", "unknown"})
        unknown = rows["unknown"]
        self.assertEqual(
            (
                unknown.input_tokens,
                unknown.output_tokens,
                unknown.cache_read_tokens,
                unknown.cache_creation_tokens,
                unknown.reasoning_tokens,
                unknown.total_tokens,
            ),
            (30, 6, 9, 3, None, 53),
        )
        self.assertIsNone(unknown.cost_amount)
        self.assertIsNone(unknown.cost_currency)
        self.assertIsNone(unknown.cost_basis)
        self.assertEqual(sum(row.total_tokens for row in rows.values()), 525)

    def test_unknown_merge_sums_nullable_values_only_when_both_are_known(self):
        def unknown_model(key: str, reasoning: int, cost: float) -> dict:
            return {
                "key": key,
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_read_tokens": 3,
                "cache_creation_tokens": 4,
                "reasoning_tokens": reasoning,
                "total_tokens": 10 + reasoning,
                "cost_usd": cost,
            }

        payload = daily_payload(
            {
                "key": "2026-07-02",
                "model_breakdown": [
                    unknown_model("(unknown)", 2, 0.25),
                    unknown_model("unknown", 3, 0.5),
                ],
            }
        )

        result = OpenUsageDailyImporter(
            runner=Mock(return_value=completed(payload)), clock=lambda: NOW,
            codex_attribution=CodexAttributionResolver(
                sessions_root=FIXTURES / "no-codex-sessions"
            ),
        ).fetch("codex", SINCE, UNTIL)

        self.assertTrue(result.ok)
        self.assertEqual(len(result.rows), 1)
        unknown = result.rows[0]
        self.assertEqual(unknown.reasoning_tokens, 5)
        self.assertEqual(unknown.cost_amount, "0.75")
        self.assertEqual(unknown.cost_currency, "USD")
        self.assertEqual(unknown.cost_basis, "price_table_estimated")
        self.assertEqual(
            (
                unknown.input_tokens,
                unknown.output_tokens,
                unknown.cache_read_tokens,
                unknown.cache_creation_tokens,
                unknown.total_tokens,
            ),
            (2, 4, 6, 8, 25),
        )

    def test_external_model_labels_get_readable_bounded_collision_safe_ids(self):
        labels = ("a/b", "a b", "Very long model / " + "x" * 300)
        models = [
            {
                "key": label,
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_read_tokens": 3,
                "cache_creation_tokens": 4,
                "reasoning_tokens": None,
                "total_tokens": 10,
                "cost_usd": None,
            }
            for label in labels
        ]
        payload = daily_payload(
            {"key": "2026-07-02", "model_breakdown": models}
        )

        result = OpenUsageDailyImporter(
            runner=Mock(return_value=completed(payload)), clock=lambda: NOW
        ).fetch("codex", SINCE, UNTIL)

        self.assertTrue(result.ok)
        ids = [row.model_id for row in result.rows]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)
        self.assertTrue(all(identifier.startswith(("a-b-", "very-long-model-")) for identifier in ids))
        self.assertTrue(all(len(identifier) <= 96 for identifier in ids))
        self.assertNotEqual(ids[0], ids[1])

    def test_out_of_range_day_is_dropped_before_nested_shape_validation(self):
        payload = daily_payload(
            {"key": "2026-06-30", "label": "outside"},
            day_payload("2026-07-02"),
        )

        result = OpenUsageDailyImporter(
            runner=Mock(return_value=completed(payload)), clock=lambda: NOW
        ).fetch("codex", SINCE, UNTIL)

        self.assertTrue(result.ok)
        self.assertEqual([row.day for row in result.rows], ["2026-07-02"])

    def test_explicit_supported_quality_is_preserved(self):
        result = OpenUsageDailyImporter(
            runner=Mock(return_value=completed(daily_payload(day_payload(quality="direct"))))
        ).fetch("codex", SINCE, UNTIL)

        self.assertTrue(result.ok)
        self.assertEqual(result.rows[0].quality, "direct")

    def test_invalid_scope_never_starts_a_process(self):
        run = Mock()
        importer = OpenUsageDailyImporter(runner=run)

        bad_provider = importer.fetch("bad provider", SINCE, UNTIL)
        reversed_range = importer.fetch("codex", UNTIL, SINCE)

        self.assertEqual(bad_provider.error_code, "invalid_request")
        self.assertEqual(reversed_range.error_code, "invalid_request")
        run.assert_not_called()

    def test_failures_are_stable_sanitized_codes(self):
        secret = "Bearer top-secret cookie=private session_key=hidden"
        failures = (
            (Mock(side_effect=OSError(secret)), "start_failed"),
            (Mock(side_effect=subprocess.TimeoutExpired(secret, 30)), "timeout"),
            (Mock(return_value=completed("", returncode=9, stderr=secret)), "command_failed"),
            (Mock(return_value=completed(secret)), "invalid_json"),
            (Mock(return_value=completed(daily_payload({"key": "2026-07-02", "model_breakdown": secret}))), "invalid_payload"),
        )
        for runner, code in failures:
            with self.subTest(code=code):
                result = OpenUsageDailyImporter(runner=runner).fetch("codex", SINCE, UNTIL)
                self.assertEqual(result, DailyImportResult(False, (), code))
                self.assertNotIn("secret", repr(result))
                self.assertNotIn("cookie", repr(result))
                self.assertNotIn("Bearer", repr(result))


class ActivityCollectorTests(unittest.TestCase):
    def test_identity_is_upserted_before_history_source_and_quota_facts(self):
        calls: list[str] = []
        store = Mock()
        store.has_daily_history.return_value = True
        store.upsert_provider_instance.side_effect = lambda *_args: calls.append("identity")
        store.replace_provider_days.side_effect = lambda *_args: calls.append("history")
        store.record_source_success.side_effect = lambda *_args: calls.append("source")
        store.record_quota.side_effect = lambda *_args: calls.append("quota")
        importer = Mock()
        importer.fetch.return_value = DailyImportResult(True, ())

        ActivityCollector(store, importer, clock=lambda: NOW).refresh(
            Overview([card("codex", remaining_percent=18)])
        )

        self.assertEqual(calls[0], "identity")
        self.assertLess(calls.index("identity"), calls.index("history"))
        self.assertLess(calls.index("identity"), calls.index("quota"))
        instance = store.upsert_provider_instance.call_args.args[0]
        self.assertEqual(
            instance,
            ProviderInstance(
                provider_id="codex",
                family_id="codex",
                display_name="Codex",
                category="subscription",
                credential_source="openusage",
                source_kind="openusage",
                observed_at="2026-07-14T02:00:00.000000Z",
            ),
        )

    def test_builtin_instance_families_are_persisted_exactly(self):
        store = Mock()
        store.has_daily_history.return_value = True
        importer = Mock()
        importer.fetch.return_value = DailyImportResult(True, ())

        ActivityCollector(store, importer, clock=lambda: NOW).refresh(
            Overview(
                [
                    card(
                        "minimax-1783978290",
                        family_id="minimax",
                        credential_source="minimax_builtin_api",
                        source_kind="builtin_api",
                    ),
                    card(
                        "step-plan-main",
                        family_id="step_plan",
                        credential_source="step_plan_official_api",
                        source_kind="official_api",
                    ),
                    card("future_agent", family_id="future_agent"),
                ]
            )
        )

        identities = {
            call.args[0].provider_id: call.args[0].family_id
            for call in store.upsert_provider_instance.call_args_list
        }
        self.assertEqual(
            identities,
            {
                "future_agent": "future_agent",
                "minimax-1783978290": "minimax",
                "step-plan-main": "step_plan",
            },
        )

    def test_identity_failure_does_not_delete_prior_identity_or_block_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            with ActivityStore(Path(directory) / "ledger.sqlite3") as store:
                prior = store.upsert_provider_instance(
                    ProviderInstance(
                        provider_id="codex",
                        family_id="codex",
                        display_name="Codex",
                        category="subscription",
                        credential_source="openusage",
                        source_kind="openusage",
                        observed_at="2026-07-14T01:00:00Z",
                    )
                )
                importer = Mock()
                importer.fetch.return_value = DailyImportResult(True, ())

                ActivityCollector(store, importer, clock=lambda: NOW).refresh(
                    Overview(
                        [
                            card(
                                "codex",
                                family_id="minimax",
                                credential_source="minimax_builtin_api",
                                source_kind="builtin_api",
                            )
                        ]
                    )
                )

                self.assertEqual(store.provider_instances(), (prior,))
                self.assertTrue(store.has_daily_history("codex"))

    def test_first_import_uses_365_days_and_sorted_unique_providers(self):
        store = Mock()
        store.has_daily_history.return_value = False
        importer = Mock()
        importer.fetch.return_value = DailyImportResult(True, ())
        collector = ActivityCollector(store, importer, clock=lambda: NOW)

        result = collector.refresh(Overview([card("zai"), card("codex"), card("zai"), card("openusage")]))

        self.assertTrue(result)
        self.assertEqual(
            [call.args for call in importer.fetch.call_args_list],
            [
                ("codex", date(2025, 7, 15), date(2026, 7, 14)),
                ("zai", date(2025, 7, 15), date(2026, 7, 14)),
            ],
        )
        store.apply_retention.assert_called_once_with(730, NOW)

    def test_existing_history_refreshes_only_current_day_for_low_latency(self):
        store = Mock()
        store.has_daily_history.return_value = True
        importer = Mock()
        importer.fetch.return_value = DailyImportResult(True, ())

        ActivityCollector(store, importer, clock=lambda: NOW).refresh(Overview([card("codex")]))

        importer.fetch.assert_called_once_with("codex", date(2026, 7, 14), date(2026, 7, 14))

    def test_refresh_uses_local_calendar_day_across_utc_boundary(self):
        store = Mock()
        store.has_daily_history.return_value = True
        importer = Mock()
        importer.fetch.return_value = DailyImportResult(True, ())
        local_midnight = datetime(
            2026, 7, 15, 0, 30, tzinfo=timezone(timedelta(hours=8))
        )

        ActivityCollector(
            store,
            importer,
            clock=lambda: local_midnight,
            local_timezone=timezone(timedelta(hours=8)),
        ).refresh(Overview([card("codex")]))

        importer.fetch.assert_called_once_with(
            "codex", date(2026, 7, 15), date(2026, 7, 15)
        )

    def test_official_success_skips_openusage_and_commits_usage_and_costs(self):
        store = Mock()
        store.has_source_success.return_value = False
        store.has_cost_history.return_value = False
        official = Mock()
        official.fetch_usage.return_value = UsageImportSuccess(
            date(2025, 7, 15),
            date(2026, 7, 14),
            (model_row(day="2026-07-14", provider_id="openai"),),
        )
        official.fetch_costs.return_value = CostImportSuccess(
            date(2025, 7, 15), date(2026, 7, 14), (cost_row(),)
        )
        openusage = Mock()

        ActivityCollector(
            store,
            openusage,
            official_importers={"openai": official},
            clock=lambda: NOW,
        ).refresh(Overview([]))

        openusage.fetch.assert_not_called()
        store.commit_usage_import_success.assert_called_once()
        store.commit_cost_import_success.assert_called_once()
        self.assertEqual(
            official.fetch_usage.call_args.args,
            (date(2025, 7, 15), date(2026, 7, 14)),
        )

    def test_provider_specific_usage_source_and_coverage_are_preserved(self):
        store = Mock()
        store.has_source_success.return_value = False
        official = Mock()
        official.usage_source_id = "minimax.billing"
        official.cost_source_id = None
        official.fetch_usage.return_value = UsageImportSuccess(
            date(2025, 7, 15),
            date(2026, 7, 13),
            (model_row(day="2026-07-13", provider_id="minimax-main"),),
        )
        openusage = Mock()

        ActivityCollector(
            store,
            openusage,
            official_importers={"minimax-main": official},
            clock=lambda: NOW,
        ).refresh(Overview([]))

        store.has_source_success.assert_called_once_with(
            "minimax-main", "minimax.billing"
        )
        store.commit_usage_import_success.assert_called_once_with(
            "minimax-main",
            "minimax.billing",
            date(2025, 7, 15),
            date(2026, 7, 13),
            official.fetch_usage.return_value.rows,
            NOW,
        )
        openusage.fetch.assert_not_called()
        official.fetch_costs.assert_not_called()

    def test_official_success_rejects_coverage_outside_requested_window(self):
        store = Mock()
        store.has_source_success.return_value = False
        official = Mock()
        official.usage_source_id = "provider.billing"
        official.cost_source_id = None
        official.fetch_usage.return_value = UsageImportSuccess(
            date(2025, 7, 14),
            date(2026, 7, 14),
            (model_row(day="2026-07-14", provider_id="provider"),),
        )

        ActivityCollector(
            store,
            Mock(),
            official_importers={"provider": official},
            clock=lambda: NOW,
        ).refresh(Overview([]))

        store.commit_usage_import_success.assert_not_called()
        store.record_source_failure.assert_any_call(
            "provider", "provider.billing", "invalid_import_rows", NOW
        )

    def test_official_failure_uses_openusage_only_during_cold_start(self):
        for had_success, expected_fallback_calls in ((False, 1), (True, 0)):
            with self.subTest(had_success=had_success):
                store = Mock()
                store.has_source_success.return_value = had_success
                store.has_cost_history.return_value = False
                official = Mock()
                official.fetch_usage.return_value = ImportFailure("rate_limited")
                official.fetch_costs.return_value = ImportFailure("rate_limited")
                openusage = Mock()
                openusage.fetch.return_value = DailyImportResult(True, ())

                ActivityCollector(
                    store,
                    openusage,
                    official_importers={"openai": official},
                    clock=lambda: NOW,
                ).refresh(Overview([]))

                self.assertEqual(openusage.fetch.call_count, expected_fallback_calls)
                store.record_source_failure.assert_any_call(
                    "openai",
                    "openai.organization.usage",
                    "rate_limited",
                    NOW,
                )
                store.record_source_failure.assert_any_call(
                    "openai",
                    "openai.organization.costs",
                    "rate_limited",
                    NOW,
                )

    def test_official_failure_preserves_last_good_rows_and_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            with ActivityStore(Path(directory) / "ledger.sqlite3") as store:
                official = Mock()
                official.fetch_usage.return_value = UsageImportSuccess(
                    date(2025, 7, 15),
                    date(2026, 7, 14),
                    (model_row(day="2026-07-14", provider_id="openai"),),
                )
                official.fetch_costs.return_value = ImportFailure("auth_required")
                openusage = Mock()
                openusage.fetch.return_value = DailyImportResult(True, ())
                collector = ActivityCollector(
                    store,
                    openusage,
                    official_importers={"openai": official},
                    clock=lambda: NOW,
                )
                collector.refresh(Overview([]))
                before = store.snapshot_daily_usage("2025-01-01", "2026-12-31")

                official.fetch_usage.return_value = ImportFailure("rate_limited")
                collector.refresh(Overview([]))
                after = store.snapshot_daily_usage("2025-01-01", "2026-12-31")

                self.assertEqual(after, before)
                openusage.fetch.assert_not_called()

    def test_failed_import_keeps_history_unchanged_and_records_only_safe_failure(self):
        store = Mock()
        store.has_daily_history.return_value = True
        importer = Mock()
        importer.fetch.return_value = DailyImportResult(False, (), "command_failed")

        ActivityCollector(store, importer, clock=lambda: NOW).refresh(Overview([card("codex")]))

        store.replace_provider_days.assert_not_called()
        store.record_source_failure.assert_called_once_with(
            "codex", "openusage.daily", "command_failed", NOW
        )

    def test_current_quota_is_written_before_slow_history_import(self):
        calls: list[str] = []
        store = Mock()
        store.has_daily_history.return_value = True
        store.replace_provider_days.side_effect = lambda *_args: calls.append("history")
        store.record_source_success.side_effect = lambda *_args: calls.append(
            "quota_source" if _args[1] == "current.quota" else "daily_source"
        )
        store.record_quota.side_effect = lambda *_args: calls.append("quota")
        store.apply_retention.side_effect = lambda *_args: calls.append("retention")
        importer = Mock()
        importer.fetch.return_value = DailyImportResult(
            True, (model_row(day="2026-07-14"),)
        )

        ActivityCollector(store, importer, clock=lambda: NOW).refresh(
            Overview([card("codex", remaining_percent=18)])
        )

        self.assertEqual(
            calls,
            ["quota", "quota_source", "history", "daily_source", "retention"],
        )
        observation = store.record_quota.call_args.args[0]
        self.assertEqual(observation.record_id, "codex.subscription")
        self.assertEqual(observation.remaining_ratio, 0.18)
        self.assertEqual(observation.remaining, "18")
        self.assertEqual(observation.observed_at, "2026-07-14T02:00:00.000000Z")
        self.assertNotIn(":", observation.record_id)

    def test_stale_numeric_quota_uses_last_good_time_and_keeps_unhealthy_source(self):
        store = Mock()
        store.has_daily_history.return_value = True
        importer = Mock()
        importer.fetch.return_value = DailyImportResult(True, ())

        ActivityCollector(store, importer, clock=lambda: NOW).refresh(
            Overview(
                [
                    card(
                        "minimax",
                        remaining_percent=18,
                        stale=True,
                        refreshed_at=OLDER,
                    )
                ]
            )
        )

        observation = store.record_quota.call_args.args[0]
        self.assertEqual(observation.observed_at, "2026-07-14T01:15:00.000000Z")
        self.assertTrue(observation.stale)
        self.assertEqual(observation.state, "stale")
        self.assertIn(
            (
                "minimax",
                "current.quota",
                "stale",
                NOW,
                "quota_unavailable",
            ),
            [call.args for call in store.record_source_status.call_args_list],
        )
        self.assertNotIn(
            ("minimax", "current.quota", NOW),
            [call.args for call in store.record_source_success.call_args_list],
        )

    def test_naive_quota_timestamp_creates_no_quota_and_safe_unavailable_health(self):
        store = Mock()
        store.has_daily_history.return_value = True
        importer = Mock()
        importer.fetch.return_value = DailyImportResult(True, ())

        ActivityCollector(store, importer, clock=lambda: NOW).refresh(
            Overview(
                [
                    card(
                        "minimax",
                        remaining_percent=18,
                        refreshed_at=datetime(2026, 7, 14, 1, 15),
                    )
                ]
            )
        )

        store.record_quota.assert_not_called()
        self.assertIn(
            (
                "minimax",
                "current.quota",
                "temporarily_unavailable",
                NOW,
                "invalid_observation_time",
            ),
            [call.args for call in store.record_source_status.call_args_list],
        )

    def test_unknown_quota_is_not_recorded_as_zero(self):
        store = Mock()
        store.has_daily_history.return_value = True
        importer = Mock()
        importer.fetch.return_value = DailyImportResult(True, ())

        ActivityCollector(store, importer, clock=lambda: NOW).refresh(
            Overview([
                card("codex", status=ProviderStatus.UNKNOWN),
                card("minimax", status=ProviderStatus.ERROR, stale=True, last_error="Bearer secret"),
            ])
        )

        store.record_quota.assert_not_called()
        current_health = [
            call.args for call in store.record_source_status.call_args_list
        ]
        self.assertEqual(
            current_health,
            [
                (
                    "codex",
                    "current.quota",
                    "temporarily_unavailable",
                    NOW,
                    "quota_unavailable",
                ),
                (
                    "minimax",
                    "current.quota",
                    "stale",
                    NOW,
                    "quota_unavailable",
                ),
            ],
        )

    def test_invalid_import_rows_are_rejected_before_storage(self):
        store = Mock()
        store.has_daily_history.return_value = True
        importer = Mock()
        importer.fetch.return_value = DailyImportResult(
            True, (model_row(provider_id="zai"),)
        )

        ActivityCollector(store, importer, clock=lambda: NOW).refresh(Overview([card("codex")]))

        store.replace_provider_days.assert_not_called()
        store.record_source_failure.assert_called_once_with(
            "codex", "openusage.daily", "invalid_import_rows", NOW
        )

    def test_one_provider_persistence_failure_does_not_block_another_or_retention(self):
        store = Mock()
        store.has_daily_history.return_value = True
        store.replace_provider_days.side_effect = [RuntimeError("cookie=secret"), None]
        importer = Mock()
        importer.fetch.return_value = DailyImportResult(True, ())

        result = ActivityCollector(store, importer, clock=lambda: NOW).refresh(
            Overview([card("codex"), card("zai")])
        )

        self.assertTrue(result)
        self.assertEqual(store.replace_provider_days.call_count, 2)
        self.assertEqual(
            store.record_source_failure.call_args_list[0].args,
            ("codex", "openusage.daily", "persistence_failed", NOW),
        )
        store.apply_retention.assert_called_once_with(730, NOW)

    def test_refresh_is_non_overlapping_and_lock_is_released(self):
        entered = threading.Event()
        release = threading.Event()
        store = Mock()
        store.has_daily_history.return_value = True
        importer = Mock()

        def wait_for_release(*_args):
            entered.set()
            release.wait(2)
            return DailyImportResult(True, ())

        importer.fetch.side_effect = wait_for_release
        collector = ActivityCollector(store, importer, clock=lambda: NOW)
        thread = threading.Thread(target=lambda: collector.refresh(Overview([card("codex")])))
        thread.start()
        self.assertTrue(entered.wait(1))

        self.assertFalse(collector.refresh(Overview([card("codex")])))
        release.set()
        thread.join(2)
        importer.fetch.side_effect = None
        importer.fetch.return_value = DailyImportResult(True, ())
        self.assertTrue(collector.refresh(Overview([])))


class ActivityStoreCollectorIntegrationTests(unittest.TestCase):
    def test_non_subscription_cards_clear_obsolete_quota_health(self):
        store = ActivityStore(":memory:")
        try:
            for provider_id in ("hermes", "deepseek"):
                store.record_source_status(
                    provider_id,
                    "current.quota",
                    "stale",
                    OLDER,
                    "quota_unavailable",
                )
            daily_importer = Mock()
            daily_importer.fetch.return_value = DailyImportResult(True, ())

            ActivityCollector(store, daily_importer, clock=lambda: NOW).refresh(
                Overview([
                    card(
                        "hermes",
                        category=Category.LOCAL,
                        status=ProviderStatus.ERROR,
                    ),
                    card(
                        "deepseek",
                        category=Category.API,
                        status=ProviderStatus.ERROR,
                    ),
                    card(
                        "mistral",
                        category=Category.API,
                        status=ProviderStatus.ERROR,
                    ),
                    card("cursor", status=ProviderStatus.ERROR),
                ])
            )

            statuses = store.source_statuses()
            quota_statuses = {
                row.provider_id: row
                for row in statuses
                if row.source_id == "current.quota"
            }
            self.assertEqual(set(quota_statuses), {"cursor", "mistral"})
            self.assertEqual(
                quota_statuses["cursor"].state,
                "temporarily_unavailable",
            )
            self.assertEqual(
                quota_statuses["mistral"].state,
                "temporarily_unavailable",
            )
            self.assertEqual(
                {
                    row.provider_id
                    for row in statuses
                    if row.source_id == "openusage.daily" and row.state == "ok"
                },
                {"cursor", "deepseek", "hermes", "mistral"},
            )
        finally:
            store.close()

    def test_openusage_raw_attributes_never_enter_ledger(self):
        private_values = (
            "private@example.test",
            "/Users/private/.config/provider",
            "d11f9ed8-1111-4222-8333-abcdeffedcba",
            "Bearer sanitized-secret-value",
        )
        overview = OpenUsageAdapter.parse(
            {
                "snapshots": [
                    {
                        "provider_id": "future_agent",
                        "status": "OK",
                        "message": "usage",
                        "metrics": {},
                        "attributes": {
                            "email": private_values[0],
                            "path": private_values[1],
                            "organization_uuid": private_values[2],
                            "authorization": private_values[3],
                        },
                    }
                ]
            },
            NOW,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite3"
            with ActivityStore(path) as store:
                importer = Mock(return_value=None)
                importer.fetch.return_value = DailyImportResult(True, ())
                ActivityCollector(store, importer, clock=lambda: NOW).refresh(overview)
                persisted = repr(
                    (
                        store.provider_instances(),
                        store.quota_states(),
                        store.source_statuses(),
                    )
                )

            database = path.read_bytes().decode("utf-8", errors="ignore")
            for value in private_values:
                self.assertNotIn(value, persisted)
                self.assertNotIn(value, database)

    def test_failed_import_does_not_change_existing_history_or_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            with ActivityStore(Path(directory) / "ledger.sqlite3") as store:
                store.replace_daily_usage("codex", "2026-07-02", (model_row(),))
                before = store.snapshot_daily_usage("2026-01-01", "2026-12-31")
                importer = Mock()
                importer.fetch.return_value = DailyImportResult(
                    False, (), "command_failed"
                )

                ActivityCollector(store, importer, clock=lambda: NOW).refresh(
                    Overview([card("codex")])
                )

                after = store.snapshot_daily_usage("2026-01-01", "2026-12-31")
                self.assertEqual(after.rows, before.rows)
                self.assertEqual(after.covered, before.covered)
                self.assertEqual(after.known_scopes, before.known_scopes)
                self.assertEqual(
                    [(item.provider_id, item.family_id) for item in store.provider_instances()],
                    [("codex", "codex")],
                )
                self.assertEqual(
                    store.source_statuses()[0].error_code, "command_failed"
                )

    def test_successful_empty_import_atomically_covers_every_day(self):
        with tempfile.TemporaryDirectory() as directory:
            with ActivityStore(Path(directory) / "ledger.sqlite3") as store:
                store.replace_provider_days("codex", SINCE, UNTIL, ())

                self.assertTrue(store.has_daily_history("codex"))
                for day in ("2026-07-01", "2026-07-02", "2026-07-03"):
                    self.assertTrue(store.is_day_covered("codex", day))
                self.assertEqual(store.daily_usage("2026-07-01", "2026-07-03"), [])

    def test_range_replacement_rolls_back_on_invalid_row(self):
        with tempfile.TemporaryDirectory() as directory:
            with ActivityStore(Path(directory) / "ledger.sqlite3") as store:
                store.replace_provider_days("codex", SINCE, UNTIL, (model_row(),))
                before = store.snapshot_daily_usage("2026-07-01", "2026-07-03")

                with self.assertRaises(ValueError):
                    store.replace_provider_days(
                        "codex", SINCE, UNTIL, (model_row(provider_id="zai"),)
                    )

                after = store.snapshot_daily_usage("2026-07-01", "2026-07-03")
                self.assertEqual(after, before)

    def test_source_status_and_retention_preserve_current_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            with ActivityStore(Path(directory) / "ledger.sqlite3") as store:
                daily_importer = Mock()
                daily_importer.fetch.return_value = DailyImportResult(True, ())
                ActivityCollector(store, daily_importer, clock=lambda: NOW).refresh(
                    Overview([card("minimax", remaining_percent=18)])
                )

                statuses = store.source_statuses()
                self.assertEqual(
                    {status.source_id: status.state for status in statuses},
                    {"current.quota": "ok", "openusage.daily": "ok"},
                )
                quotas = store.quota_states()
                self.assertEqual(len(quotas), 1)
                self.assertEqual(quotas[0].remaining_ratio, 0.18)

    def test_unavailable_current_quota_is_persisted_only_as_source_health(self):
        with tempfile.TemporaryDirectory() as directory:
            with ActivityStore(Path(directory) / "ledger.sqlite3") as store:
                daily_importer = Mock()
                daily_importer.fetch.return_value = DailyImportResult(True, ())

                ActivityCollector(store, daily_importer, clock=lambda: NOW).refresh(
                    Overview(
                        [
                            card(
                                "minimax",
                                status=ProviderStatus.ERROR,
                                last_error="Bearer top-secret cookie=private",
                            )
                        ]
                    )
                )

                self.assertEqual(store.quota_states(), [])
                current = next(
                    status
                    for status in store.source_statuses()
                    if status.source_id == "current.quota"
                )
                self.assertEqual(current.state, "temporarily_unavailable")
                self.assertEqual(current.error_code, "quota_unavailable")
                self.assertNotIn("secret", repr(current))
                self.assertNotIn("cookie", repr(current))

    def test_current_quota_source_recovers_after_later_ok_numeric_card(self):
        with tempfile.TemporaryDirectory() as directory:
            with ActivityStore(Path(directory) / "ledger.sqlite3") as store:
                daily_importer = Mock()
                daily_importer.fetch.return_value = DailyImportResult(True, ())
                collector = ActivityCollector(store, daily_importer, clock=lambda: NOW)

                collector.refresh(
                    Overview([card("minimax", status=ProviderStatus.ERROR)])
                )
                collector.refresh(
                    Overview([card("minimax", remaining_percent=18)])
                )

                current = next(
                    status
                    for status in store.source_statuses()
                    if status.source_id == "current.quota"
                )
                self.assertEqual(current.state, "ok")
                self.assertEqual(current.last_attempt_at, "2026-07-14T02:00:00.000000Z")
                self.assertEqual(current.last_success_at, "2026-07-14T02:00:00.000000Z")
                self.assertEqual(current.stale_at, "2026-07-14T02:05:00.000000Z")
                self.assertIsNone(current.error_code)

    def test_older_worker_cannot_downgrade_newer_quota_source_health(self):
        cases = (
            card(
                "cursor",
                remaining_percent=75,
                stale=True,
                refreshed_at=datetime(2026, 7, 14, 0, 55, tzinfo=timezone.utc),
            ),
            card("cursor", status=ProviderStatus.ERROR),
            card("cursor", remaining_percent=75, refreshed_at=NOW.replace(tzinfo=None)),
        )
        for older_card in cases:
            with self.subTest(status=older_card.status, quota=older_card.remaining_percent):
                store = ActivityStore(":memory:")
                daily_importer = Mock()
                daily_importer.fetch.return_value = DailyImportResult(True, ())
                ActivityCollector(
                    store, daily_importer, clock=lambda: NOW
                ).refresh(
                    Overview([
                        card(
                            "cursor",
                            remaining_percent=100,
                            refreshed_at=NOW,
                        )
                    ])
                )
                ActivityCollector(
                    store,
                    daily_importer,
                    clock=lambda: datetime(
                        2026, 7, 14, 1, 0, tzinfo=timezone.utc
                    ),
                ).refresh(Overview([older_card]))

                quota = store.quota_states()[0]
                self.assertEqual(
                    quota.observed_at, "2026-07-14T02:00:00.000000Z"
                )
                self.assertEqual(quota.remaining_ratio, 1.0)
                self.assertFalse(quota.stale)
                current = next(
                    status
                    for status in store.source_statuses()
                    if status.source_id == "current.quota"
                )
                self.assertEqual(current.state, "ok")
                self.assertEqual(
                    current.last_attempt_at, "2026-07-14T02:00:00.000000Z"
                )
                self.assertEqual(
                    current.stale_at, "2026-07-14T02:05:00.000000Z"
                )
                self.assertIsNone(current.error_code)
                store.close()

    def test_older_quota_persistence_failure_cannot_downgrade_newer_health(self):
        store = ActivityStore(":memory:")
        try:
            daily_importer = Mock()
            daily_importer.fetch.return_value = DailyImportResult(True, ())
            ActivityCollector(store, daily_importer, clock=lambda: NOW).refresh(
                Overview([card("cursor", remaining_percent=100)])
            )
            with patch.object(
                store, "record_quota", side_effect=RuntimeError("write failed")
            ):
                ActivityCollector(
                    store,
                    daily_importer,
                    clock=lambda: datetime(
                        2026, 7, 14, 1, 0, tzinfo=timezone.utc
                    ),
                ).refresh(Overview([card("cursor", remaining_percent=75)]))

            current = next(
                status
                for status in store.source_statuses()
                if status.source_id == "current.quota"
            )
            self.assertEqual(current.state, "ok")
            self.assertEqual(
                current.last_attempt_at, "2026-07-14T02:00:00.000000Z"
            )
            self.assertIsNone(current.error_code)
        finally:
            store.close()

    def test_stale_numeric_quota_persists_old_observation_and_current_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            with ActivityStore(Path(directory) / "ledger.sqlite3") as store:
                daily_importer = Mock()
                daily_importer.fetch.return_value = DailyImportResult(True, ())

                ActivityCollector(store, daily_importer, clock=lambda: NOW).refresh(
                    Overview(
                        [
                            card(
                                "minimax",
                                remaining_percent=18,
                                stale=True,
                                refreshed_at=OLDER,
                            )
                        ]
                    )
                )

                quota_state = store.quota_states()[0]
                self.assertEqual(
                    quota_state.observed_at, "2026-07-14T01:15:00.000000Z"
                )
                current = next(
                    status
                    for status in store.source_statuses()
                    if status.source_id == "current.quota"
                )
                self.assertEqual(current.state, "stale")
                self.assertEqual(
                    current.last_attempt_at, "2026-07-14T02:00:00.000000Z"
                )


if __name__ == "__main__":
    unittest.main()
