from __future__ import annotations

import importlib
import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


BRIDGE_MODES = ("loom-stdio", "codex-stdio", "claude-code-stdio")
COLLECTOR_REPORT = {
    "apiVersion": "plugin-self-test/v1",
    "object": "plugin.server_self_test",
    "ok": True,
    "synthetic": True,
    "checks": {
        "principalIsolation": True,
        "samePayloadReplay": True,
        "differentPayloadConflict": True,
        "restartReplay": True,
    },
}
EXPECTED_REPORT = {
    "apiVersion": "plugin-self-test/v1",
    "object": "plugin.self_test",
    "ok": True,
    "synthetic": True,
    "checks": {
        "principalIsolation": True,
        "bridgeModes": list(BRIDGE_MODES),
        "samePayloadReplay": True,
        "differentPayloadConflict": True,
        "restartReplay": True,
    },
}


def bridge_report(mode: str) -> dict[str, object]:
    return {
        "apiVersion": "plugin-self-test/v1",
        "object": "plugin.bridge_self_test",
        "ok": True,
        "synthetic": True,
        "mode": mode,
    }


def smoke_plugin_api():
    return importlib.import_module("scripts.smoke_plugin_api")


class PluginPackagedSmokeTests(unittest.TestCase):
    def test_runs_one_server_and_three_bridge_synthetic_contracts(self) -> None:
        module = smoke_plugin_api()
        collector = "/private/build/openusage-collector"
        bridge = "/private/build/openusage-plugin-bridge"
        calls: list[list[str]] = []

        def successful_runner(command, **_kwargs):
            calls.append(command)
            if command[1] == "__plugin-self-test":
                payload = COLLECTOR_REPORT
            else:
                payload = bridge_report(command[1])
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(payload),
                stderr="",
            )

        report = module.run_frozen_plugin_smoke(
            collector,
            bridge,
            command_runner=successful_runner,
        )

        self.assertEqual(report, EXPECTED_REPORT)
        self.assertEqual(
            calls,
            [
                [collector, "__plugin-self-test", "--format", "json"],
                [bridge, "loom-stdio", "--self-test", "--format", "json"],
                [bridge, "codex-stdio", "--self-test", "--format", "json"],
                [
                    bridge,
                    "claude-code-stdio",
                    "--self-test",
                    "--format",
                    "json",
                ],
            ],
        )

    def test_rejects_non_exact_or_ambiguous_reports_without_disclosure(self) -> None:
        module = smoke_plugin_api()
        collector = "/Users/private/CANARY-collector"
        bridge = "/Users/private/CANARY-bridge"
        invalid_reports = (
            "not-json",
            json.dumps({**COLLECTOR_REPORT, "privatePath": collector}),
            json.dumps(COLLECTOR_REPORT) + json.dumps(COLLECTOR_REPORT),
            '{"ok":true,"ok":true}',
            '{"ok":NaN}',
            "[" * 10_000 + "0" + "]" * 10_000,
        )

        for output in invalid_reports:
            with self.subTest(output_prefix=output[:32]):
                def invalid_runner(command, **_kwargs):
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=output,
                        stderr="",
                    )

                with self.assertRaises(ValueError) as raised:
                    module.run_frozen_plugin_smoke(
                        collector,
                        bridge,
                        command_runner=invalid_runner,
                    )
                self.assertEqual(
                    str(raised.exception),
                    "packaged Plugin self-test failed",
                )
                self.assertNotIn("CANARY", str(raised.exception))

    def test_rejects_process_failure_stderr_and_oversized_output(self) -> None:
        module = smoke_plugin_api()

        def process_failure(command, **_kwargs):
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="CANARY_PRIVATE_ERROR",
            )

        def oversized(command, **_kwargs):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="x" * (64 * 1024 + 1),
                stderr="",
            )

        for runner in (process_failure, oversized):
            with self.subTest(runner=runner.__name__):
                with self.assertRaises(ValueError) as raised:
                    module.run_frozen_plugin_smoke(
                        "/private/collector",
                        "/private/bridge",
                        command_runner=runner,
                    )
                self.assertEqual(
                    str(raised.exception),
                    "packaged Plugin self-test failed",
                )

    def test_rejects_relative_or_control_character_binary_paths(self) -> None:
        module = smoke_plugin_api()
        invalid_pairs = (
            ("openusage-collector", "/private/bridge"),
            ("/private/collector", "./openusage-plugin-bridge"),
            ("/private/collector\nCANARY", "/private/bridge"),
            ("/private/collector", "/private/bridge\x7fCANARY"),
        )

        for collector, bridge in invalid_pairs:
            with self.subTest(collector=collector, bridge=bridge):
                calls: list[list[str]] = []

                def must_not_run(command, **_kwargs):
                    calls.append(command)
                    payload = (
                        COLLECTOR_REPORT
                        if command[1] == "__plugin-self-test"
                        else bridge_report(command[1])
                    )
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=json.dumps(payload),
                        stderr="",
                    )

                with self.assertRaises(ValueError) as raised:
                    module.run_frozen_plugin_smoke(
                        collector,
                        bridge,
                        command_runner=must_not_run,
                    )
                self.assertEqual(
                    str(raised.exception),
                    "packaged Plugin self-test failed",
                )
                self.assertNotIn("CANARY", str(raised.exception))
                self.assertEqual(calls, [])

    def test_cli_requires_both_binaries_and_emits_only_canonical_result(self) -> None:
        module = smoke_plugin_api()
        output = io.StringIO()
        error = io.StringIO()
        with (
            patch.object(
                module,
                "run_frozen_plugin_smoke",
                return_value=EXPECTED_REPORT,
            ) as run,
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            status = module.main(
                [
                    "--collector",
                    "/private/collector",
                    "--bridge",
                    "/private/bridge",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(run.call_args.args, ("/private/collector", "/private/bridge"))
        self.assertEqual(json.loads(output.getvalue()), EXPECTED_REPORT)
        self.assertEqual(error.getvalue(), "")

    def test_cli_collapses_private_failures_to_one_safe_error(self) -> None:
        module = smoke_plugin_api()
        output = io.StringIO()
        error = io.StringIO()
        with (
            patch.object(
                module,
                "run_frozen_plugin_smoke",
                side_effect=ValueError("CANARY /Users/private token"),
            ),
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            status = module.main(
                [
                    "--collector",
                    "/Users/private/collector",
                    "--bridge",
                    "/Users/private/bridge",
                ]
            )

        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(error.getvalue(), "packaged Plugin smoke failed\n")
        self.assertNotIn("CANARY", error.getvalue())


if __name__ == "__main__":
    unittest.main()
