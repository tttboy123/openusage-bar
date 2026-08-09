import importlib
import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


ADVISE_FIXTURE = {
    "request": {
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "estimated_tokens": 512,
        "window": "5m",
    },
    "snapshot": {
        "remaining_ratio": 0.8,
        "quota_remaining": None,
        "burn_rate_per_minute": None,
        "unknown": False,
    },
}

GATEWAY_FIXTURE = {
    "provider": "openai",
    "model": "gpt-4.1-mini",
    "stream": False,
    "request": {"input": "offline smoke fixture"},
}

EXPECTED_FROZEN_REPORT = {
    "schemaVersion": "gateway-self-test/v1",
    "object": "gateway.self_test",
    "ok": True,
    "checks": {
        "observe": {
            "ok": True,
            "observer": "healthy",
            "gateway": "disabled",
            "credentialReads": 0,
            "providerCalls": 0,
        },
        "advise": {
            "ok": True,
            "decision": "yes",
            "reason": "quota_healthy",
            "credentialReads": 0,
            "providerCalls": 0,
        },
        "gateway": {
            "ok": True,
            "status": "complete",
            "credentialReads": 1,
            "providerCalls": 1,
        },
        "credentialFailure": {
            "ok": True,
            "errorCode": "credential_unavailable",
            "retryable": False,
            "providerCalls": 0,
            "observer": "healthy",
        },
    },
}


def smoke_gateway():
    return importlib.import_module("scripts.smoke_gateway")


class GatewaySmokeTests(unittest.TestCase):
    def test_install_starts_offline_with_observer_healthy_and_gateway_disabled(self):
        calls = {"provider": 0, "credential": 0}

        def provider_call(*_args, **_kwargs):
            calls["provider"] += 1
            raise AssertionError("observe mode must not call a Provider")

        def credential_read(*_args, **_kwargs):
            calls["credential"] += 1
            raise AssertionError("observe mode must not read a credential")

        report = smoke_gateway().run_smoke(
            "observe",
            provider_call=provider_call,
            credential_read=credential_read,
        )

        self.assertEqual(
            report,
            {
                "mode": "observe",
                "local_api": "healthy",
                "gateway": "disabled",
                "should_send": None,
                "provider_calls": 0,
                "credential_reads": 0,
            },
        )
        self.assertEqual(calls, {"provider": 0, "credential": 0})

    def test_advise_evaluates_the_should_send_fixture_without_private_io(self):
        calls = {"provider": 0, "credential": 0}

        def provider_call(*_args, **_kwargs):
            calls["provider"] += 1
            raise AssertionError("advise mode must not call a Provider")

        def credential_read(*_args, **_kwargs):
            calls["credential"] += 1
            raise AssertionError("advise mode must not read a credential")

        report = smoke_gateway().run_smoke(
            "advise",
            fixture=ADVISE_FIXTURE,
            provider_call=provider_call,
            credential_read=credential_read,
        )

        self.assertEqual(report["mode"], "advise")
        self.assertEqual(report["local_api"], "healthy")
        self.assertEqual(report["gateway"], "healthy")
        self.assertEqual(
            report["should_send"],
            {"decision": "yes", "reason": "quota_healthy"},
        )
        self.assertEqual(report["provider_calls"], 0)
        self.assertEqual(report["credential_reads"], 0)
        self.assertEqual(calls, {"provider": 0, "credential": 0})

    def test_gateway_smoke_uses_only_the_injected_provider_and_credential_seams(self):
        calls = {"provider": [], "credential": []}

        def credential_read(*args, **kwargs):
            calls["credential"].append((args, kwargs))
            return "offline-test-credential"

        def provider_call(*args, **kwargs):
            calls["provider"].append((args, kwargs))
            return {
                "status": 200,
                "body": {
                    "id": "offline-response",
                    "output": [],
                },
            }

        report = smoke_gateway().run_smoke(
            "gateway",
            fixture=GATEWAY_FIXTURE,
            provider_call=provider_call,
            credential_read=credential_read,
        )

        self.assertEqual(report["mode"], "gateway")
        self.assertEqual(report["local_api"], "healthy")
        self.assertEqual(report["gateway"], "healthy")
        self.assertIsNone(report["should_send"])
        self.assertEqual(report["provider_calls"], 1)
        self.assertEqual(report["credential_reads"], 1)
        self.assertEqual(len(calls["provider"]), 1)
        self.assertEqual(len(calls["credential"]), 1)

    def test_expected_states_keeps_the_documented_observe_report(self):
        self.assertEqual(
            smoke_gateway().expected_states(),
            {
                "local_api": "healthy",
                "gateway": "disabled",
                "provider_calls": 0,
                "credential_reads": 0,
            },
        )

    def test_frozen_collector_self_test_invokes_exact_binary_and_strictly_validates_json(self):
        module = smoke_gateway()
        run_frozen = getattr(module, "run_frozen_collector_smoke", None)
        self.assertIsNotNone(
            run_frozen,
            "missing public exact-frozen-collector self-test boundary",
        )
        collector = "/tmp/just-built/openusage-collector"
        calls = []

        def successful_runner(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(EXPECTED_FROZEN_REPORT),
                stderr="",
            )

        reports = run_frozen(collector, command_runner=successful_runner)

        self.assertEqual(reports, EXPECTED_FROZEN_REPORT)
        self.assertEqual(
            calls,
            [[
                collector,
                "__gateway-self-test",
                "--format",
                "json",
            ]],
        )

        invalid_outputs = (
            "not-json",
            json.dumps({**EXPECTED_FROZEN_REPORT, "unexpected": True}),
            json.dumps({
                **EXPECTED_FROZEN_REPORT,
                "checks": {
                    **EXPECTED_FROZEN_REPORT["checks"],
                    "gateway": {
                        **EXPECTED_FROZEN_REPORT["checks"]["gateway"],
                        "providerCalls": 2,
                    },
                },
            }),
        )
        for stdout in invalid_outputs:
            with self.subTest(stdout=stdout):
                def invalid_runner(command, **_kwargs):
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=stdout,
                        stderr="",
                    )

                with self.assertRaises(ValueError):
                    run_frozen(collector, command_runner=invalid_runner)

    def test_deeply_nested_frozen_report_fails_without_traceback_or_path(self):
        module = smoke_gateway()
        collector = "/Users/private/just-built/openusage-collector"
        payload = "[" * 10_000 + "0" + "]" * 10_000
        self.assertEqual(len(payload.encode("utf-8")), 20_001)

        def deep_report_runner(command, **_kwargs):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=payload,
                stderr="",
            )

        with self.assertRaises(ValueError) as raised:
            module.run_frozen_collector_smoke(
                collector,
                command_runner=deep_report_runner,
            )
        self.assertNotIn("RecursionError", str(raised.exception))
        self.assertNotIn(collector, str(raised.exception))

        original = module.run_frozen_collector_smoke

        def run_deep_report(collector_path):
            return original(
                collector_path,
                command_runner=deep_report_runner,
            )

        output = io.StringIO()
        error = io.StringIO()
        with (
            patch.object(
                module,
                "run_frozen_collector_smoke",
                new=run_deep_report,
            ),
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            result = module.main(["--all", "--collector", collector])

        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(error.getvalue(), "frozen collector smoke failed\n")
        for private_value in ("Traceback", "RecursionError", collector, "/Users/"):
            with self.subTest(private_value=private_value):
                self.assertNotIn(private_value, error.getvalue())


if __name__ == "__main__":
    unittest.main()
