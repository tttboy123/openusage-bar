#!/usr/bin/env python3
"""Offline smoke probes for the Observer-first optional Gateway modes.

The module exposes injected seams so CI can prove exactly which modes may read
credentials or invoke a Provider without touching a real keychain or network.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openusage_bar.gateway.contracts import ShouldSendRequest
from openusage_bar.gateway.policy import PolicySnapshot, ShouldSendPolicy


_ADVISE_FIXTURE: dict[str, object] = {
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

_GATEWAY_FIXTURE: dict[str, object] = {
    "provider": "openai",
    "model": "gpt-4.1-mini",
    "stream": False,
    "request": {"input": "offline smoke fixture"},
}

_EXPECTED_FROZEN_REPORT: dict[str, object] = {
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
_MAX_FROZEN_REPORT_BYTES = 64 * 1024
_FROZEN_SELF_TEST_TIMEOUT_SECONDS = 30


def expected_states() -> dict[str, object]:
    """Document the safe state of a fresh install in Observe mode."""

    return {
        "local_api": "healthy",
        "gateway": "disabled",
        "provider_calls": 0,
        "credential_reads": 0,
    }


def run_smoke(
    mode: str,
    *,
    fixture: Mapping[str, object] | None = None,
    provider_call: Callable[..., object] | None = None,
    credential_read: Callable[..., object] | None = None,
) -> dict[str, object]:
    """Run one deterministic smoke probe through injected private-I/O seams."""

    if mode not in {"observe", "advise", "gateway"}:
        raise ValueError("unsupported smoke mode")

    calls = {"provider": 0, "credential": 0}

    def call_provider(*args: object, **kwargs: object) -> object:
        calls["provider"] += 1
        if provider_call is None:
            return {"status": 200, "body": {"id": "offline-response"}}
        return provider_call(*args, **kwargs)

    def read_credential(*args: object, **kwargs: object) -> object:
        calls["credential"] += 1
        if credential_read is None:
            return "offline-test-credential"
        return credential_read(*args, **kwargs)

    should_send: dict[str, str] | None = None
    gateway_state = "disabled" if mode == "observe" else "healthy"

    if mode == "advise":
        request_payload, snapshot_payload = _advise_payloads(fixture)
        decision = ShouldSendPolicy().decide(
            ShouldSendRequest(
                provider=_required_string(request_payload, "provider"),
                model=_required_string(request_payload, "model"),
                estimated_tokens=_required_integer(
                    request_payload, "estimated_tokens"
                ),
                window=_required_string(request_payload, "window"),
            ),
            PolicySnapshot(
                remaining_ratio=_optional_number(
                    snapshot_payload, "remaining_ratio"
                ),
                quota_remaining=_optional_number(
                    snapshot_payload, "quota_remaining"
                ),
                burn_rate_per_minute=_optional_number(
                    snapshot_payload, "burn_rate_per_minute"
                ),
                unknown=_required_boolean(snapshot_payload, "unknown"),
            ),
        )
        should_send = {
            "decision": decision.decision.value,
            "reason": decision.reason,
        }
    elif mode == "gateway":
        gateway_fixture = _mapping(fixture, "gateway fixture")
        provider = _required_string(gateway_fixture, "provider")
        model = _required_string(gateway_fixture, "model")
        credential = read_credential(provider)
        call_provider(
            gateway_fixture,
            provider=provider,
            model=model,
            credential=credential,
        )

    return {
        "mode": mode,
        "local_api": "healthy",
        "gateway": gateway_state,
        "should_send": should_send,
        "provider_calls": calls["provider"],
        "credential_reads": calls["credential"],
    }


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid frozen collector self-test report")
        result[key] = value
    return result


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def run_frozen_collector_smoke(
    collector: str | Path,
    *,
    command_runner: Callable[..., object] = subprocess.run,
) -> dict[str, object]:
    """Run and strictly validate one exact frozen Collector executable."""

    collector_value = str(collector)
    if (
        not collector_value
        or collector_value != collector_value.strip()
        or "\x00" in collector_value
    ):
        raise ValueError("invalid frozen collector")
    command = [
        collector_value,
        "__gateway-self-test",
        "--format",
        "json",
    ]
    try:
        completed = command_runner(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=_FROZEN_SELF_TEST_TIMEOUT_SECONDS,
        )
        returncode = getattr(completed, "returncode")
        stdout = getattr(completed, "stdout")
        stderr = getattr(completed, "stderr")
    except Exception:
        raise ValueError("frozen collector self-test failed") from None
    if (
        type(returncode) is not int
        or returncode != 0
        or type(stdout) is not str
        or type(stderr) is not str
        or stderr
    ):
        raise ValueError("frozen collector self-test failed")
    try:
        if len(stdout.encode("utf-8")) > _MAX_FROZEN_REPORT_BYTES:
            raise ValueError
        parsed = json.loads(
            stdout,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        if (
            type(parsed) is not dict
            or _canonical_json(parsed) != _canonical_json(_EXPECTED_FROZEN_REPORT)
        ):
            raise ValueError
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise ValueError("invalid frozen collector self-test report") from None
    return parsed


def _advise_payloads(
    fixture: Mapping[str, object] | None,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    value = _mapping(fixture, "advise fixture")
    return (
        _mapping(value.get("request"), "advise request"),
        _mapping(value.get("snapshot"), "advise snapshot"),
    )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"invalid {label}")
    return value


def _required_string(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError("invalid smoke fixture")
    return result


def _required_integer(value: Mapping[str, object], key: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise ValueError("invalid smoke fixture")
    return result


def _required_boolean(value: Mapping[str, object], key: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        raise ValueError("invalid smoke fixture")
    return result


def _optional_number(value: Mapping[str, object], key: str) -> float | None:
    result = value.get(key)
    if result is None:
        return None
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise ValueError("invalid smoke fixture")
    return float(result)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smoke_gateway.py")
    parser.add_argument("--all", action="store_true", help="run all three modes")
    parser.add_argument("--collector", help="exact frozen Collector executable")
    parsed = parser.parse_args(arguments)
    if not parsed.all:
        parser.error("--all is required")

    if parsed.collector is not None:
        try:
            report = run_frozen_collector_smoke(parsed.collector)
        except ValueError:
            sys.stderr.write("frozen collector smoke failed\n")
            return 1
        print(_canonical_json(report))
        return 0

    reports = [
        run_smoke("observe"),
        run_smoke("advise", fixture=_ADVISE_FIXTURE),
        run_smoke("gateway", fixture=_GATEWAY_FIXTURE),
    ]
    print(_canonical_json(reports))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
