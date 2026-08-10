#!/usr/bin/env python3
"""Strict packaged smoke for the synthetic Plugin server and stdio bridges."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path


_API_VERSION = "plugin-self-test/v1"
_BRIDGE_MODES = ("loom-stdio", "codex-stdio", "claude-code-stdio")
_MAX_REPORT_BYTES = 64 * 1024
_PROCESS_TIMEOUT_SECONDS = 30
_GENERIC_SELF_TEST_ERROR = "packaged Plugin self-test failed"

_EXPECTED_COLLECTOR_REPORT: dict[str, object] = {
    "apiVersion": _API_VERSION,
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

_EXPECTED_REPORT: dict[str, object] = {
    "apiVersion": _API_VERSION,
    "object": "plugin.self_test",
    "ok": True,
    "synthetic": True,
    "checks": {
        "principalIsolation": True,
        "bridgeModes": list(_BRIDGE_MODES),
        "samePayloadReplay": True,
        "differentPayloadConflict": True,
        "restartReplay": True,
    },
}


def _expected_bridge_report(mode: str) -> dict[str, object]:
    return {
        "apiVersion": _API_VERSION,
        "object": "plugin.bridge_self_test",
        "ok": True,
        "synthetic": True,
        "mode": mode,
    }


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validated_binary(value: str | Path) -> str:
    try:
        result = str(value)
    except Exception:
        raise ValueError(_GENERIC_SELF_TEST_ERROR) from None
    if (
        not result
        or len(result) > 4096
        or result != result.strip()
        or not os.path.isabs(result)
        or any(ord(character) < 32 or ord(character) == 127 for character in result)
    ):
        raise ValueError(_GENERIC_SELF_TEST_ERROR)
    return result


def _run_exact_report(
    command: list[str],
    expected: dict[str, object],
    *,
    command_runner: Callable[..., object],
) -> None:
    try:
        completed = command_runner(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=_PROCESS_TIMEOUT_SECONDS,
        )
        returncode = getattr(completed, "returncode")
        stdout = getattr(completed, "stdout")
        stderr = getattr(completed, "stderr")
        if (
            type(returncode) is not int
            or returncode != 0
            or type(stdout) is not str
            or type(stderr) is not str
            or stderr
            or len(stdout.encode("utf-8")) > _MAX_REPORT_BYTES
        ):
            raise ValueError
        parsed = json.loads(
            stdout,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        if type(parsed) is not dict or _canonical_json(parsed) != _canonical_json(
            expected
        ):
            raise ValueError
    except Exception:
        raise ValueError(_GENERIC_SELF_TEST_ERROR) from None


def run_frozen_plugin_smoke(
    collector: str | Path,
    bridge: str | Path,
    *,
    command_runner: Callable[..., object] = subprocess.run,
) -> dict[str, object]:
    """Run strict synthetic Plugin checks through two exact packaged binaries."""

    collector_binary = _validated_binary(collector)
    bridge_binary = _validated_binary(bridge)
    _run_exact_report(
        [collector_binary, "__plugin-self-test", "--format", "json"],
        _EXPECTED_COLLECTOR_REPORT,
        command_runner=command_runner,
    )
    for mode in _BRIDGE_MODES:
        _run_exact_report(
            [bridge_binary, mode, "--self-test", "--format", "json"],
            _expected_bridge_report(mode),
            command_runner=command_runner,
        )
    return json.loads(_canonical_json(_EXPECTED_REPORT))


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smoke_plugin_api.py")
    parser.add_argument("--collector", required=True)
    parser.add_argument("--bridge", required=True)
    parsed = parser.parse_args(arguments)
    try:
        report = run_frozen_plugin_smoke(parsed.collector, parsed.bridge)
    except ValueError:
        sys.stderr.write("packaged Plugin smoke failed\n")
        return 1
    print(_canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
