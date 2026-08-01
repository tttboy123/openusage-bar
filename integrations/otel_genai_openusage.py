"""Privacy-bounded OpenTelemetry GenAI Span exporter for OpenUsage Bar.

This module intentionally does not expose an OTLP receiver. It runs inside the
instrumented process and serializes only an explicit GenAI allowlist into the
existing runtime-observation/v1 Collector contract.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


try:
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
except ImportError:
    class SpanExporter:  # type: ignore[no-redef]
        """Dependency-free base used while auditing this standalone file."""

        pass

    class SpanExportResult(Enum):  # type: ignore[no-redef]
        SUCCESS = 0
        FAILURE = 1


SOURCE_ID = "otel.genai.f77b923.v1"
SCHEMA_VERSION = 1
MAX_BATCH_SIZE = 256
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_COUNTER = 1_000_000_000_000
MAX_DURATION_NS = 24 * 60 * 60 * 1_000_000_000

_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SCOPE_REF = re.compile(r"^anon_[0-9a-f]{16,64}$")

_OPERATION = "gen_ai.operation.name"
_PROVIDER = "gen_ai.provider.name"
_REQUEST_MODEL = "gen_ai.request.model"
_RESPONSE_MODEL = "gen_ai.response.model"
_INPUT = "gen_ai.usage.input_tokens"
_OUTPUT = "gen_ai.usage.output_tokens"
_CACHE_READ = "gen_ai.usage.cache_read.input_tokens"
_CACHE_CREATION = "gen_ai.usage.cache_creation.input_tokens"
_REASONING = "gen_ai.usage.reasoning.output_tokens"


def _counter(value: object, *, default: int | None = None) -> int | None:
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_COUNTER
    ):
        return None
    return value


def _timestamp(value: object) -> str | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        return None
    seconds, nanos = divmod(value, 1_000_000_000)
    try:
        parsed = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
            microsecond=nanos // 1000
        )
    except (OverflowError, OSError, ValueError):
        return None
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _valid_provider_map(value: object) -> bool:
    return (
        isinstance(value, dict)
        and bool(value)
        and all(
            isinstance(source, str)
            and _STABLE_ID.fullmatch(source) is not None
            and isinstance(target, str)
            and _STABLE_ID.fullmatch(target) is not None
            for source, target in value.items()
        )
    )


def _identity(
    attributes: Mapping[str, object], provider_map: dict[str, str]
) -> tuple[str, str] | None:
    operation = attributes.get(_OPERATION)
    source_provider = attributes.get(_PROVIDER)
    raw_model = attributes.get(_RESPONSE_MODEL) or attributes.get(_REQUEST_MODEL)
    if (
        not isinstance(operation, str)
        or _STABLE_ID.fullmatch(operation) is None
        or not isinstance(source_provider, str)
        or not isinstance(raw_model, str)
    ):
        return None
    provider_id = provider_map.get(source_provider)
    model_part = raw_model.replace("/", ".")
    if (
        not isinstance(provider_id, str)
        or _STABLE_ID.fullmatch(provider_id) is None
        or _STABLE_ID.fullmatch(model_part) is None
    ):
        return None
    model_id = (
        model_part
        if model_part == provider_id or model_part.startswith(provider_id + ".")
        else f"{provider_id}.{model_part}"
    )
    if _STABLE_ID.fullmatch(model_id) is None:
        return None
    return provider_id, model_id


def _usage(
    attributes: Mapping[str, object],
) -> tuple[int, int, int, int, int | None, int] | None:
    input_tokens = _counter(attributes.get(_INPUT))
    output_tokens = _counter(attributes.get(_OUTPUT))
    cache_read_tokens = _counter(attributes.get(_CACHE_READ), default=0)
    cache_creation_tokens = _counter(attributes.get(_CACHE_CREATION), default=0)
    raw_reasoning = attributes.get(_REASONING)
    reasoning_tokens = (
        None if raw_reasoning is None else _counter(raw_reasoning)
    )
    if (
        input_tokens is None
        or output_tokens is None
        or cache_read_tokens is None
        or cache_creation_tokens is None
        or raw_reasoning is not None
        and reasoning_tokens is None
        or cache_read_tokens + cache_creation_tokens > input_tokens
        or reasoning_tokens is not None
        and reasoning_tokens > output_tokens
    ):
        return None
    total_tokens = input_tokens + output_tokens
    if total_tokens > MAX_COUNTER:
        return None
    return (
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens,
        reasoning_tokens,
        total_tokens,
    )


def _status(span: object) -> str:
    status = getattr(span, "status", None)
    code = getattr(status, "status_code", None)
    name = getattr(code, "name", None)
    if name is None and isinstance(code, str):
        name = code
    return "error" if name == "ERROR" else "completed"


def _observation_id(span: object, id_salt: bytes) -> str | None:
    context = getattr(span, "context", None)
    trace_id = getattr(context, "trace_id", None)
    span_id = getattr(context, "span_id", None)
    if (
        isinstance(trace_id, bool)
        or not isinstance(trace_id, int)
        or not 0 < trace_id < 1 << 128
        or isinstance(span_id, bool)
        or not isinstance(span_id, int)
        or not 0 < span_id < 1 << 64
    ):
        return None
    opaque = trace_id.to_bytes(16, "big") + span_id.to_bytes(8, "big")
    digest = hmac.new(id_salt, opaque, hashlib.sha256).hexdigest()[:32]
    return f"obs_{digest}"


def _observation(
    span: object,
    *,
    scope_ref: str,
    provider_map: dict[str, str],
    id_salt: bytes,
) -> dict[str, object] | None:
    attributes = getattr(span, "attributes", None)
    if not isinstance(attributes, Mapping):
        return None
    identity = _identity(attributes, provider_map)
    counters = _usage(attributes)
    observation_id = _observation_id(span, id_salt)
    start_ns = getattr(span, "start_time", None)
    end_ns = getattr(span, "end_time", None)
    if (
        identity is None
        or counters is None
        or observation_id is None
        or isinstance(start_ns, bool)
        or not isinstance(start_ns, int)
        or isinstance(end_ns, bool)
        or not isinstance(end_ns, int)
        or end_ns < start_ns
        or end_ns - start_ns > MAX_DURATION_NS
    ):
        return None
    started_at = _timestamp(start_ns)
    completed_at = _timestamp(end_ns)
    if started_at is None or completed_at is None:
        return None
    provider_id, model_id = identity
    (
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens,
        reasoning_tokens,
        total_tokens,
    ) = counters
    return {
        "observationId": observation_id,
        "providerId": provider_id,
        "modelId": model_id,
        "scopeRef": scope_ref,
        "startedAt": started_at,
        "firstTokenAt": None,
        "completedAt": completed_at,
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "cacheReadTokens": cache_read_tokens,
        "cacheCreationTokens": cache_creation_tokens,
        "reasoningTokens": reasoning_tokens,
        "totalTokens": total_tokens,
        "tokenCountingConvention": "input_includes_cache",
        "status": _status(span),
        "costMicros": None,
        "costCurrency": None,
        "sourceId": SOURCE_ID,
        "quality": "provider_reported",
    }


def build_runtime_document(
    spans: object,
    *,
    scope_ref: str,
    provider_map: dict[str, str],
    id_salt: bytes,
) -> dict[str, object] | None:
    """Reduce a bounded batch of completed GenAI spans to schema v1."""

    if (
        not isinstance(spans, Sequence)
        or isinstance(spans, (str, bytes, bytearray))
        or not 0 < len(spans) <= MAX_BATCH_SIZE
        or not isinstance(scope_ref, str)
        or _SCOPE_REF.fullmatch(scope_ref) is None
        or not _valid_provider_map(provider_map)
        or not isinstance(id_salt, bytes)
        or not 16 <= len(id_salt) <= 64
    ):
        return None
    observations: list[dict[str, object]] = []
    for span in spans:
        row = _observation(
            span,
            scope_ref=scope_ref,
            provider_map=provider_map,
            id_salt=id_salt,
        )
        if row is None:
            return None
        observations.append(row)
    return {"schemaVersion": SCHEMA_VERSION, "observations": observations}


def _child_environment() -> dict[str, str]:
    return {
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }


class OpenUsageGenAISpanExporter(SpanExporter):
    """Best-effort in-process exporter that never forwards original spans."""

    def __init__(
        self,
        *,
        collector_path: str | Path,
        database_path: str | Path,
        scope_ref: str,
        provider_map: dict[str, str],
        id_salt: bytes | None = None,
        runner: Any = subprocess.run,
    ) -> None:
        collector = Path(collector_path)
        database = Path(database_path)
        selected_salt = secrets.token_bytes(32) if id_salt is None else id_salt
        if (
            not collector.is_absolute()
            or collector.is_symlink()
            or not collector.is_file()
            or not os.access(collector, os.X_OK)
            or not database.is_absolute()
            or database.is_symlink()
            or not database.parent.is_dir()
            or not isinstance(scope_ref, str)
            or _SCOPE_REF.fullmatch(scope_ref) is None
            or not _valid_provider_map(provider_map)
            or not isinstance(selected_salt, bytes)
            or not 16 <= len(selected_salt) <= 64
            or not callable(runner)
        ):
            raise ValueError("invalid OpenUsage GenAI exporter configuration")
        self._collector = collector
        self._database = database
        self._scope_ref = scope_ref
        self._provider_map = dict(provider_map)
        self._id_salt = bytes(selected_salt)
        self._runner = runner

    def export(self, spans: Sequence[object]) -> SpanExportResult:
        document = build_runtime_document(
            spans,
            scope_ref=self._scope_ref,
            provider_map=self._provider_map,
            id_salt=self._id_salt,
        )
        if document is None:
            return SpanExportResult.FAILURE
        try:
            encoded = json.dumps(
                document,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            return SpanExportResult.FAILURE
        if len(encoded.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            return SpanExportResult.FAILURE
        try:
            completed = self._runner(
                [
                    str(self._collector),
                    "runtime-ingest",
                    "--database",
                    str(self._database),
                ],
                input=encoded,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                shell=False,
                check=False,
                timeout=3,
                close_fds=True,
                env=_child_environment(),
            )
            if getattr(completed, "returncode", None) == 0:
                return SpanExportResult.SUCCESS
        except Exception:
            pass
        return SpanExportResult.FAILURE

    def shutdown(self, timeout_millis: int = 30_000) -> None:
        del timeout_millis

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True
