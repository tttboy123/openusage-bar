#!/usr/bin/env python3
"""Exercise the packaged GenAI Span Adapter through the packaged Collector."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any


MAX_BYTES = 1024 * 1024
SOURCE_ID = "otel.genai.f77b923.v1"


def _regular(path: Path, *, executable: bool = False) -> bool:
    return (
        path.is_absolute()
        and not path.is_symlink()
        and path.is_file()
        and (not executable or os.access(path, os.X_OK))
    )


def _load(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(
        "packaged_openusage_otel_genai", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("adapter unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_summary(collector: Path, database: Path) -> tuple[dict[str, Any], bytes]:
    completed = subprocess.run(
        [
            str(collector),
            "runtime-summary",
            "--database",
            str(database),
            "--window-seconds",
            "60",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=False,
        shell=False,
        check=False,
        timeout=15,
        close_fds=True,
        env={
            "HOME": str(Path.home()),
            "LANG": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        },
    )
    if completed.returncode != 0 or len(completed.stdout) > MAX_BYTES:
        raise RuntimeError("adapter summary unavailable")
    payload = json.loads(completed.stdout.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("adapter summary invalid")
    return payload, completed.stdout


def _private_values(fixture: dict[str, Any]) -> tuple[bytes, ...]:
    attributes = fixture["attributes"]
    values = (
        attributes["gen_ai.input.messages"],
        attributes["gen_ai.output.messages"],
        attributes["gen_ai.tool.call.arguments"],
        attributes["enduser.id"],
        attributes["session.id"],
        attributes["api.key"],
        fixture["events"][0]["body"],
        fixture["links"][0]["traceId"],
        fixture["resource"]["service.name"],
        fixture["traceId"],
        fixture["spanId"],
    )
    return tuple(str(value).encode("utf-8") for value in values)


def verify(collector: Path, integration: Path, fixture_path: Path) -> None:
    if (
        not _regular(collector, executable=True)
        or not _regular(integration)
        or not _regular(fixture_path)
        or integration.parent.stat().st_mode & 0o222
        or fixture_path.stat().st_size > MAX_BYTES
    ):
        raise ValueError("adapter smoke input unavailable")
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(fixture, dict):
        raise ValueError("adapter smoke fixture invalid")
    integration_module = _load(integration)
    if (integration.parent / "__pycache__").exists():
        raise RuntimeError("adapter modified the signed integration directory")
    completed_ns = time.time_ns()
    started_ns = completed_ns - 3_000_000_000
    span = SimpleNamespace(
        context=SimpleNamespace(
            trace_id=int(str(fixture["traceId"]), 16),
            span_id=int(str(fixture["spanId"]), 16),
        ),
        start_time=started_ns,
        end_time=completed_ns,
        attributes=dict(fixture["attributes"]),
        status=SimpleNamespace(status_code=SimpleNamespace(name=fixture["status"])),
    )

    with tempfile.TemporaryDirectory(prefix="openusage-adapter-smoke-") as directory:
        database = Path(directory) / "runtime.sqlite3"
        exporter = integration_module.OpenUsageGenAISpanExporter(
            collector_path=collector,
            database_path=database,
            scope_ref="anon_0123456789abcdef",
            provider_map={"openai": "openai"},
            id_salt=b"openusage-packaged-adapter-smoke",
        )
        result = exporter.export([span])
        if result != integration_module.SpanExportResult.SUCCESS:
            raise RuntimeError("adapter delivery failed")
        summary, summary_bytes = _run_summary(collector, database)
        groups = summary.get("groups")
        tokens = summary.get("tokens")
        costs = summary.get("costs")
        latency = summary.get("latency")
        if (
            summary.get("schemaVersion") != 1
            or summary.get("runtimeRevision") != 1
            or summary.get("observationCount") != 1
            or not isinstance(tokens, dict)
            or tokens.get("total") != 15
            or costs != []
            or not isinstance(latency, dict)
            or latency.get("ttftSampleCount") != 0
            or not isinstance(groups, list)
            or len(groups) != 1
            or groups[0].get("providerId") != "openai"
            or groups[0].get("modelId") != "openai.gpt-5-2026-07-01"
        ):
            raise RuntimeError("adapter summary invalid")
        if (database.stat().st_mode & 0o777) != (
            stat.S_IRUSR | stat.S_IWUSR
        ):
            raise RuntimeError("adapter database is not private")
        persisted = database.read_bytes()
        for value in _private_values(fixture):
            if value in persisted or value in summary_bytes:
                raise RuntimeError("adapter private material persisted")
    if (integration.parent / "__pycache__").exists():
        raise RuntimeError("adapter modified the signed integration directory")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collector", type=Path, required=True)
    parser.add_argument("--integration", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        verify(args.collector, args.integration, args.fixture)
    except Exception:
        print("runtime_adapter_smoke_failed", file=sys.stderr)
        return 1
    print(
        "runtime_adapter_smoke_ok "
        f"producer={SOURCE_ID} observations=1"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
