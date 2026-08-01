#!/usr/bin/env python3
"""Exercise packaged Runtime Observation commands through the signed launcher."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


MAX_OUTPUT_BYTES = 1024 * 1024
FORBIDDEN_KEYS = frozenset({
    "prompt",
    "response",
    "credential",
    "cookie",
    "apikey",
    "requestid",
    "accountref",
    "rawpayload",
    "email",
})


def _json_output(value: str) -> dict[str, Any]:
    if len(value.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise ValueError("runtime smoke output is oversized")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("runtime smoke output is invalid")
    return parsed


def _private_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).replace("_", "").lower() in FORBIDDEN_KEYS
            or _private_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_private_key(child) for child in value)
    return False


def _run(command: list[str], *, payload: str | None = None) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        input=payload,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
        timeout=15,
        env=dict(os.environ),
    )
    if completed.returncode != 0 or completed.stderr:
        raise RuntimeError("packaged runtime command failed")
    return _json_output(completed.stdout)


def verify(collector: Path, fixture: Path) -> None:
    if (
        not collector.is_absolute()
        or collector.is_symlink()
        or not collector.is_file()
        or not os.access(collector, os.X_OK)
        or not fixture.is_absolute()
        or fixture.is_symlink()
        or not fixture.is_file()
    ):
        raise ValueError("runtime smoke input is unavailable")
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    observations = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(observations, list) or len(observations) != 1:
        raise ValueError("runtime smoke fixture is invalid")
    observation = observations[0]
    if not isinstance(observation, dict):
        raise ValueError("runtime smoke fixture is invalid")
    completed_at = datetime.now(timezone.utc).replace(microsecond=0)
    observation["startedAt"] = (
        completed_at - timedelta(seconds=3)
    ).isoformat().replace("+00:00", "Z")
    observation["firstTokenAt"] = (
        completed_at - timedelta(seconds=2)
    ).isoformat().replace("+00:00", "Z")
    observation["completedAt"] = completed_at.isoformat().replace("+00:00", "Z")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    with tempfile.TemporaryDirectory(prefix="openusage-runtime-smoke-") as directory:
        database = Path(directory) / "runtime.sqlite3"
        ingest = _run([
            str(collector),
            "runtime-ingest",
            "--database",
            str(database),
        ], payload=encoded)
        if ingest != {
            "acceptedCount": 1,
            "duplicateCount": 0,
            "expiredCount": 0,
            "prunedCount": 0,
            "runtimeRevision": 1,
            "schemaVersion": 1,
        }:
            raise RuntimeError("packaged runtime ingest result is invalid")
        summary = _run([
            str(collector),
            "runtime-summary",
            "--database",
            str(database),
            "--window-seconds",
            "60",
        ])
        groups = summary.get("groups")
        tokens = summary.get("tokens")
        if (
            summary.get("schemaVersion") != 1
            or summary.get("runtimeRevision") != 1
            or summary.get("observationCount") != 1
            or not isinstance(tokens, dict)
            or tokens.get("total") != 20
            or not isinstance(groups, list)
            or len(groups) != 1
            or groups[0].get("providerId") != "openai"
            or groups[0].get("modelId") != "gpt-5"
            or _private_key(summary)
        ):
            raise RuntimeError("packaged runtime summary is invalid")
        if (database.stat().st_mode & 0o777) != (stat.S_IRUSR | stat.S_IWUSR):
            raise RuntimeError("packaged runtime database is not private")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collector", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        verify(args.collector, args.fixture)
    except Exception:
        print("runtime_observation_smoke_failed", file=sys.stderr)
        return 1
    print("runtime_observation_smoke_ok observations=1 revision=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
