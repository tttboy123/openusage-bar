import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "integrations" / "otel_genai_openusage.py"
FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "runtime-producers"
    / "otel-genai-f77b923-success-v1.json"
)
SCOPE_REF = "anon_0123456789abcdef"
ID_SALT = b"openusage-otel-fixture-id-salt"


def load_integration():
    spec = importlib.util.spec_from_file_location(
        "openusage_otel_genai_integration", INTEGRATION
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("integration module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class Poison:
    """Fails the test if a non-allowlisted span surface is inspected."""

    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"private span surface accessed: {name}")

    def __str__(self) -> str:
        raise AssertionError("private span surface serialized")


def fixture_span(payload: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        name=payload["name"],
        context=SimpleNamespace(
            trace_id=int(str(payload["traceId"]), 16),
            span_id=int(str(payload["spanId"]), 16),
        ),
        start_time=int(str(payload["startTimeUnixNano"])),
        end_time=int(str(payload["endTimeUnixNano"])),
        attributes=dict(payload["attributes"]),
        status=SimpleNamespace(status_code=SimpleNamespace(name=payload["status"])),
        events=Poison(),
        links=Poison(),
        resource=Poison(),
        instrumentation_scope=Poison(),
    )


class Completed:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


class RecordingRunner:
    def __init__(self, *, returncode: int = 0, error: Exception | None = None) -> None:
        self.returncode = returncode
        self.error = error
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, command: list[str], **kwargs: object) -> Completed:
        self.calls.append((command, kwargs))
        if self.error is not None:
            raise self.error
        return Completed(self.returncode)


class OTelGenAIRuntimeTransformTests(unittest.TestCase):
    def test_content_bearing_span_becomes_one_strict_private_observation(self):
        from openusage_bar.runtime_observation import decode_runtime_document

        integration = load_integration()
        fixture = load_fixture()

        result = integration.build_runtime_document(
            [fixture_span(fixture)],
            scope_ref=SCOPE_REF,
            provider_map={"openai": "openai"},
            id_salt=ID_SALT,
        )

        self.assertIsNotNone(result)
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        decoded = decode_runtime_document(encoded)
        self.assertEqual(len(decoded.observations), 1)
        row = decoded.observations[0]
        self.assertRegex(row.observation_id, r"^obs_[0-9a-f]{32}$")
        self.assertEqual(row.provider_id, "openai")
        self.assertEqual(row.model_id, "openai.gpt-5-2026-07-01")
        self.assertEqual(row.scope_ref, SCOPE_REF)
        self.assertEqual(row.input_tokens, 12)
        self.assertEqual(row.output_tokens, 3)
        self.assertEqual(row.cache_read_tokens, 4)
        self.assertEqual(row.cache_creation_tokens, 0)
        self.assertEqual(row.reasoning_tokens, 1)
        self.assertEqual(row.total_tokens, 15)
        self.assertEqual(row.token_counting_convention, "input_includes_cache")
        self.assertIsNone(row.first_token_at)
        self.assertEqual(row.status, "completed")
        self.assertIsNone(row.cost_micros)
        self.assertIsNone(row.cost_currency)
        self.assertEqual(row.source_id, "otel.genai.f77b923.v1")
        self.assertEqual(row.quality, "provider_reported")
        for private in (
            "private fixture prompt",
            "private fixture response",
            "private tool arguments",
            "private-user@example.com",
            "private-session-id",
            "fixture-provider-key",
            "private event body",
            "private-linked-trace-id",
            "private-service-identity",
            str(fixture["traceId"]),
            str(fixture["spanId"]),
        ):
            with self.subTest(private=private):
                self.assertNotIn(private, encoded)

    def test_missing_or_inconsistent_usage_is_dropped_not_zero_filled(self):
        integration = load_integration()
        fixture = load_fixture()
        base = fixture_span(fixture)
        cases = (
            {"gen_ai.usage.input_tokens": None},
            {"gen_ai.usage.output_tokens": None},
            {"gen_ai.usage.input_tokens": True},
            {"gen_ai.usage.cache_read.input_tokens": 13},
            {"gen_ai.usage.reasoning.output_tokens": 4},
            {"gen_ai.usage.output_tokens": -1},
            {
                "gen_ai.usage.input_tokens": 0,
                "gen_ai.usage.output_tokens": 0,
                "gen_ai.usage.cache_read.input_tokens": 0,
                "gen_ai.usage.reasoning.output_tokens": 0,
            },
        )

        for changes in cases:
            with self.subTest(changes=changes):
                attributes = dict(base.attributes)
                for key, value in changes.items():
                    if value is None:
                        attributes.pop(key, None)
                    else:
                        attributes[key] = value
                span = SimpleNamespace(**{**base.__dict__, "attributes": attributes})
                self.assertIsNone(integration.build_runtime_document(
                    [span],
                    scope_ref=SCOPE_REF,
                    provider_map={"openai": "openai"},
                    id_salt=ID_SALT,
                ))

    def test_unknown_provider_unsafe_model_scope_or_time_fails_closed(self):
        integration = load_integration()
        fixture = load_fixture()
        base = fixture_span(fixture)
        cases = (
            ([base], {}, SCOPE_REF),
            ([base], {"openai": "openai/account"}, SCOPE_REF),
            ([base], {"openai": "openai"}, "account@example.com"),
            ([SimpleNamespace(**{**base.__dict__, "end_time": base.start_time - 1})], {"openai": "openai"}, SCOPE_REF),
            ([SimpleNamespace(**{**base.__dict__, "end_time": None})], {"openai": "openai"}, SCOPE_REF),
        )

        for spans, provider_map, scope_ref in cases:
            with self.subTest(provider_map=provider_map, scope_ref=scope_ref):
                self.assertIsNone(integration.build_runtime_document(
                    spans,
                    scope_ref=scope_ref,
                    provider_map=provider_map,
                    id_salt=ID_SALT,
                ))

        attributes = dict(base.attributes)
        attributes["gen_ai.response.model"] = "model with spaces"
        unsafe = SimpleNamespace(**{**base.__dict__, "attributes": attributes})
        self.assertIsNone(integration.build_runtime_document(
            [unsafe],
            scope_ref=SCOPE_REF,
            provider_map={"openai": "openai"},
            id_salt=ID_SALT,
        ))

    def test_batch_is_bounded_and_invalid_member_drops_whole_batch(self):
        integration = load_integration()
        fixture = load_fixture()
        span = fixture_span(fixture)

        self.assertIsNone(integration.build_runtime_document(
            [span] * 257,
            scope_ref=SCOPE_REF,
            provider_map={"openai": "openai"},
            id_salt=ID_SALT,
        ))
        invalid = SimpleNamespace(**{**span.__dict__, "attributes": {}})
        self.assertIsNone(integration.build_runtime_document(
            [span, invalid],
            scope_ref=SCOPE_REF,
            provider_map={"openai": "openai"},
            id_salt=ID_SALT,
        ))


class OTelGenAIRuntimeDeliveryTests(unittest.TestCase):
    def exporter(self, root: Path, runner: RecordingRunner):
        integration = load_integration()
        collector = root / "OpenUsage Collector"
        collector.write_text("fixture", encoding="utf-8")
        collector.chmod(0o700)
        database = root / "runtime.sqlite3"
        exporter = integration.OpenUsageGenAISpanExporter(
            collector_path=collector,
            database_path=database,
            scope_ref=SCOPE_REF,
            provider_map={"openai": "openai"},
            id_salt=ID_SALT,
            runner=runner,
        )
        return integration, exporter, collector, database

    def test_export_uses_bounded_stdin_and_a_credential_free_child(self):
        fixture = load_fixture()
        runner = RecordingRunner()
        private_environment = {
            "OPENAI_API_KEY": "private-provider-key",
            "ANTHROPIC_API_KEY": "private-anthropic-key",
            "COOKIE": "private-cookie",
        }

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, private_environment, clear=False
        ):
            integration, exporter, collector, database = self.exporter(
                Path(directory), runner
            )
            result = exporter.export([fixture_span(fixture)])

        self.assertEqual(result, integration.SpanExportResult.SUCCESS)
        self.assertEqual(len(runner.calls), 1)
        command, options = runner.calls[0]
        self.assertEqual(command, [
            str(collector), "runtime-ingest", "--database", str(database)
        ])
        self.assertIs(options["stdout"], subprocess.DEVNULL)
        self.assertIs(options["stderr"], subprocess.DEVNULL)
        self.assertEqual(options["timeout"], 3)
        self.assertEqual(options["shell"], False)
        self.assertEqual(options["check"], False)
        self.assertEqual(options["text"], True)
        self.assertEqual(options["close_fds"], True)
        child_environment = options["env"]
        self.assertIsInstance(child_environment, dict)
        for key in private_environment:
            self.assertNotIn(key, child_environment)
        encoded = options["input"]
        self.assertIsInstance(encoded, str)
        self.assertLessEqual(len(encoded.encode("utf-8")), 1024 * 1024)
        for private in private_environment.values():
            self.assertNotIn(private, encoded)

    def test_invalid_span_or_delivery_failure_is_silent(self):
        fixture = load_fixture()
        invalid = fixture_span(fixture)
        invalid.attributes = {}
        runners = (
            RecordingRunner(returncode=1),
            RecordingRunner(error=subprocess.TimeoutExpired(["collector"], 3)),
        )

        with tempfile.TemporaryDirectory() as directory:
            integration, exporter, _, _ = self.exporter(
                Path(directory), RecordingRunner()
            )
            self.assertEqual(
                exporter.export([invalid]), integration.SpanExportResult.FAILURE
            )

        for runner in runners:
            with self.subTest(runner=runner), tempfile.TemporaryDirectory() as directory:
                integration, exporter, _, _ = self.exporter(Path(directory), runner)
                self.assertEqual(
                    exporter.export([fixture_span(fixture)]),
                    integration.SpanExportResult.FAILURE,
                )

    def test_constructor_rejects_relative_symlink_or_identity_configuration(self):
        integration = load_integration()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root / "collector"
            collector.write_text("fixture", encoding="utf-8")
            collector.chmod(0o700)
            database = root / "runtime.sqlite3"
            cases = (
                (Path("collector"), database, SCOPE_REF, {"openai": "openai"}),
                (collector, Path("runtime.sqlite3"), SCOPE_REF, {"openai": "openai"}),
                (collector, database, "user@example.com", {"openai": "openai"}),
                (collector, database, SCOPE_REF, {}),
            )
            for selected_collector, selected_database, scope, provider_map in cases:
                with self.subTest(collector=selected_collector, database=selected_database):
                    with self.assertRaises(ValueError):
                        integration.OpenUsageGenAISpanExporter(
                            collector_path=selected_collector,
                            database_path=selected_database,
                            scope_ref=scope,
                            provider_map=provider_map,
                        )


if __name__ == "__main__":
    unittest.main()
