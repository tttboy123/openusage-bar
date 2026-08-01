import asyncio
import contextlib
import io
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "integrations" / "litellm_openusage.py"
FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "runtime-producers"
    / "litellm-success-v1.json"
)
SCOPE_REF = "anon_0123456789abcdef"


def load_integration():
    spec = importlib.util.spec_from_file_location(
        "openusage_litellm_integration", INTEGRATION
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("integration module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def fixture_times(payload: dict[str, object]) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(str(payload["startTime"]).replace("Z", "+00:00"))
    end = datetime.fromisoformat(str(payload["endTime"]).replace("Z", "+00:00"))
    return start, end


class AttributeValue:
    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


def as_attributes(value: object) -> object:
    if isinstance(value, dict):
        return AttributeValue(**{key: as_attributes(child) for key, child in value.items()})
    if isinstance(value, list):
        return [as_attributes(child) for child in value]
    return value


class LiteLLMRuntimeTransformTests(unittest.TestCase):
    def test_content_bearing_success_becomes_one_strict_private_observation(self):
        from openusage_bar.runtime_observation import decode_runtime_document

        integration = load_integration()
        fixture = load_fixture()
        start, end = fixture_times(fixture)

        result = integration.build_runtime_document(
            fixture["kwargs"],
            fixture["response"],
            start,
            end,
            scope_ref=SCOPE_REF,
            provider_map={"openai": "openai"},
            status="completed",
        )

        self.assertIsNotNone(result)
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
        decoded = decode_runtime_document(encoded)
        self.assertEqual(len(decoded.observations), 1)
        row = decoded.observations[0]
        self.assertRegex(row.observation_id, r"^obs_[0-9a-f]{32}$")
        self.assertEqual(row.provider_id, "openai")
        self.assertEqual(row.model_id, "openai.gpt-5")
        self.assertEqual(row.scope_ref, SCOPE_REF)
        self.assertEqual(row.input_tokens, 12)
        self.assertEqual(row.output_tokens, 3)
        self.assertEqual(row.cache_read_tokens, 4)
        self.assertEqual(row.cache_creation_tokens, 0)
        self.assertEqual(row.reasoning_tokens, 1)
        self.assertEqual(row.total_tokens, 15)
        self.assertEqual(row.token_counting_convention, "input_includes_cache")
        self.assertIsNone(row.first_token_at)
        self.assertEqual(row.cost_micros, 25)
        self.assertEqual(row.cost_currency, "usd")
        self.assertEqual(row.source_id, "litellm.callback.v1")
        self.assertEqual(row.quality, "estimated")
        for private in (
            "private fixture prompt",
            "private fixture response",
            "fixture-redacted-value",
            "private failure text",
            "upstream-call-id-not-for-storage",
            "upstream-response-id-not-for-storage",
        ):
            with self.subTest(private=private):
                self.assertNotIn(private, encoded)

    def test_attribute_style_usage_and_known_first_token_time_are_supported(self):
        from openusage_bar.runtime_observation import decode_runtime_document

        integration = load_integration()
        fixture = load_fixture()
        start, end = fixture_times(fixture)
        kwargs = dict(fixture["kwargs"])
        kwargs["completion_start_time"] = start + timedelta(milliseconds=750)

        result = integration.build_runtime_document(
            kwargs,
            as_attributes(fixture["response"]),
            start,
            end,
            scope_ref=SCOPE_REF,
            provider_map={"openai": "openai"},
            status="completed",
        )

        self.assertIsNotNone(result)
        decoded = decode_runtime_document(json.dumps(result))
        self.assertEqual(decoded.observations[0].ttft_ms, 750)

    def test_missing_or_inconsistent_usage_never_becomes_zero(self):
        integration = load_integration()
        fixture = load_fixture()
        start, end = fixture_times(fixture)
        response = dict(fixture["response"])

        for usage in (
            None,
            {},
            {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 16},
            {"prompt_tokens": True, "completion_tokens": 3, "total_tokens": 4},
            {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "total_tokens": 15,
                "completion_tokens_details": {"reasoning_tokens": "private"},
            },
        ):
            with self.subTest(usage=usage):
                candidate = dict(response)
                candidate["usage"] = usage
                self.assertIsNone(integration.build_runtime_document(
                    fixture["kwargs"],
                    candidate,
                    start,
                    end,
                    scope_ref=SCOPE_REF,
                    provider_map={"openai": "openai"},
                    status="completed",
                ))

    def test_unsafe_identity_model_scope_or_time_fails_closed(self):
        integration = load_integration()
        fixture = load_fixture()
        start, end = fixture_times(fixture)
        cases = (
            ({"openai": "openai/account"}, SCOPE_REF, start, end),
            ({}, SCOPE_REF, start, end),
            ({"openai": "openai"}, "account@example.com", start, end),
            ({"openai": "openai"}, SCOPE_REF, start.replace(tzinfo=None), end),
            ({"openai": "openai"}, SCOPE_REF, end, start),
        )

        for provider_map, scope_ref, selected_start, selected_end in cases:
            with self.subTest(
                provider_map=provider_map,
                scope_ref=scope_ref,
                start=selected_start,
                end=selected_end,
            ):
                self.assertIsNone(integration.build_runtime_document(
                    fixture["kwargs"],
                    fixture["response"],
                    selected_start,
                    selected_end,
                    scope_ref=scope_ref,
                    provider_map=provider_map,
                    status="completed",
                ))

        unsafe_model = dict(fixture["kwargs"])
        unsafe_model["model"] = "openai/model with spaces"
        self.assertIsNone(integration.build_runtime_document(
            unsafe_model,
            fixture["response"],
            start,
            end,
            scope_ref=SCOPE_REF,
            provider_map={"openai": "openai"},
            status="completed",
        ))

    def test_scope_generator_returns_unlinked_canonical_values(self):
        integration = load_integration()

        first = integration.create_scope_ref()
        second = integration.create_scope_ref()

        self.assertRegex(first, r"^anon_[0-9a-f]{32}$")
        self.assertRegex(second, r"^anon_[0-9a-f]{32}$")
        self.assertNotEqual(first, second)


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


class LiteLLMRuntimeDeliveryTests(unittest.TestCase):
    def logger(self, root: Path, runner: RecordingRunner):
        integration = load_integration()
        collector = root / "OpenUsage Collector"
        collector.write_text("fixture", encoding="utf-8")
        collector.chmod(0o700)
        database = root / "runtime.sqlite3"
        return integration.OpenUsageRuntimeLogger(
            collector_path=collector,
            database_path=database,
            scope_ref=SCOPE_REF,
            provider_map={"openai": "openai"},
            runner=runner,
        ), collector, database

    def test_success_delivery_uses_bounded_stdin_and_a_credential_free_child(self):
        fixture = load_fixture()
        start, end = fixture_times(fixture)
        runner = RecordingRunner()
        private_environment = {
            "OPENAI_API_KEY": "private-provider-key",
            "ANTHROPIC_API_KEY": "private-anthropic-key",
            "OASIS_TOKEN": "private-session",
            "COOKIE": "private-cookie",
        }

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, private_environment, clear=False
        ):
            logger, collector, database = self.logger(Path(directory), runner)
            delivered = logger.log_success_event(
                fixture["kwargs"], fixture["response"], start, end
            )

        self.assertTrue(delivered)
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
        self.assertNotIn("private fixture prompt", encoded)
        self.assertNotIn("private fixture response", encoded)

    def test_missing_usage_does_not_launch_or_invent_zero(self):
        fixture = load_fixture()
        start, end = fixture_times(fixture)
        response = dict(fixture["response"])
        response["usage"] = None
        runner = RecordingRunner()

        with tempfile.TemporaryDirectory() as directory:
            logger, _, _ = self.logger(Path(directory), runner)
            delivered = logger.log_success_event(
                fixture["kwargs"], response, start, end
            )

        self.assertFalse(delivered)
        self.assertEqual(runner.calls, [])

    def test_delivery_failure_is_silent_and_never_breaks_the_model_request(self):
        fixture = load_fixture()
        start, end = fixture_times(fixture)
        runners = (
            RecordingRunner(returncode=1),
            RecordingRunner(error=subprocess.TimeoutExpired(["collector"], 3)),
            RecordingRunner(error=RuntimeError("private provider failure")),
        )

        for runner in runners:
            with self.subTest(runner=runner), tempfile.TemporaryDirectory() as directory:
                logger, _, _ = self.logger(Path(directory), runner)
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    delivered = logger.log_success_event(
                        fixture["kwargs"], fixture["response"], start, end
                    )
                self.assertFalse(delivered)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")

    def test_sync_async_and_failure_hooks_share_the_same_private_delivery(self):
        fixture = load_fixture()
        start, end = fixture_times(fixture)
        runner = RecordingRunner()

        with tempfile.TemporaryDirectory() as directory:
            logger, _, _ = self.logger(Path(directory), runner)
            self.assertTrue(logger.log_success_event(
                fixture["kwargs"], fixture["response"], start, end
            ))
            self.assertTrue(asyncio.run(logger.async_log_success_event(
                fixture["kwargs"], fixture["response"], start, end
            )))
            self.assertTrue(logger.log_failure_event(
                fixture["kwargs"], fixture["response"], start, end
            ))
            self.assertTrue(asyncio.run(logger.async_log_failure_event(
                fixture["kwargs"], fixture["response"], start, end
            )))

        self.assertEqual(len(runner.calls), 4)
        payloads = [json.loads(call[1]["input"]) for call in runner.calls]
        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(payloads[2], payloads[3])
        self.assertEqual(payloads[0]["observations"][0]["status"], "completed")
        self.assertEqual(payloads[2]["observations"][0]["status"], "error")
        encoded = json.dumps(payloads)
        self.assertNotIn("private failure text", encoded)
        self.assertNotIn("exception", encoded.lower())

    def test_invalid_collector_or_database_paths_fail_before_delivery(self):
        integration = load_integration()
        runner = RecordingRunner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root / "collector"
            collector.write_text("fixture", encoding="utf-8")
            collector.chmod(0o700)
            symlink = root / "collector-link"
            symlink.symlink_to(collector)
            cases = (
                (Path("relative-collector"), root / "runtime.sqlite3"),
                (symlink, root / "runtime.sqlite3"),
                (collector, Path("relative-runtime.sqlite3")),
            )
            for collector_path, database_path in cases:
                with self.subTest(
                    collector=collector_path, database=database_path
                ), self.assertRaises(ValueError):
                    integration.OpenUsageRuntimeLogger(
                        collector_path=collector_path,
                        database_path=database_path,
                        scope_ref=SCOPE_REF,
                        provider_map={"openai": "openai"},
                        runner=runner,
                    )


if __name__ == "__main__":
    unittest.main()
