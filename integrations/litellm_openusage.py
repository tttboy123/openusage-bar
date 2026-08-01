"""Privacy-bounded LiteLLM to OpenUsage Runtime Observation adapter.

This file is standalone by design: it can be loaded by a LiteLLM environment
without installing OpenUsage Bar as a Python package.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import re
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:
    class CustomLogger:  # type: ignore[no-redef]
        """Dependency-free base used when this file is audited outside LiteLLM."""

        pass


SOURCE_ID = "litellm.callback.v1"
SCHEMA_VERSION = 1
MAX_COUNTER = 1_000_000_000_000
MAX_DURATION = timedelta(hours=24)

_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SCOPE_REF = re.compile(r"^anon_[0-9a-f]{16,64}$")
_STATUSES = frozenset({"completed", "error", "cancelled", "timeout"})


def create_scope_ref() -> str:
    """Return an unlinked anonymous scope for one local account or route."""

    return f"anon_{secrets.token_hex(16)}"


def _read(value: object, key: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(key)
    try:
        return getattr(value, key, None)
    except Exception:
        return None


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


def _canonical_time(value: object) -> tuple[datetime, str] | None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        return None
    utc = value.astimezone(timezone.utc)
    return utc, utc.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _provider_and_model(
    kwargs: object,
    provider_map: dict[str, str],
) -> tuple[str, str] | None:
    if not isinstance(provider_map, dict) or not provider_map:
        return None
    model_value = _read(kwargs, "model")
    if not isinstance(model_value, str) or not model_value:
        return None
    provider_value = _read(kwargs, "custom_llm_provider")
    if provider_value is None:
        params = _read(kwargs, "litellm_params")
        provider_value = _read(params, "custom_llm_provider")
    if provider_value is None and "/" in model_value:
        provider_value = model_value.split("/", 1)[0]
    if not isinstance(provider_value, str):
        return None
    provider_id = provider_map.get(provider_value)
    model_id = model_value.replace("/", ".")
    if (
        not isinstance(provider_id, str)
        or _STABLE_ID.fullmatch(provider_id) is None
        or _STABLE_ID.fullmatch(model_id) is None
    ):
        return None
    return provider_id, model_id


def _cost(kwargs: object) -> tuple[int | None, str | None, str] | None:
    raw = _read(kwargs, "response_cost")
    if raw is None:
        return None, None, "provider_reported"
    if isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    micros = int((value * Decimal(1_000_000)).to_integral_value(
        rounding=ROUND_HALF_UP
    ))
    if micros > MAX_COUNTER:
        return None
    return micros, "usd", "estimated"


def _usage(response_obj: object) -> tuple[int, int, int, int, int | None, int] | None:
    usage = _read(response_obj, "usage")
    if usage is None:
        return None
    input_tokens = _counter(
        _read(usage, "prompt_tokens")
        if _read(usage, "prompt_tokens") is not None
        else _read(usage, "input_tokens")
    )
    output_tokens = _counter(
        _read(usage, "completion_tokens")
        if _read(usage, "completion_tokens") is not None
        else _read(usage, "output_tokens")
    )
    total_tokens = _counter(_read(usage, "total_tokens"))
    if input_tokens is None or output_tokens is None or total_tokens is None:
        return None
    if total_tokens != input_tokens + output_tokens:
        return None

    prompt_details = _read(usage, "prompt_tokens_details")
    completion_details = _read(usage, "completion_tokens_details")
    explicit_cache_read = _read(usage, "cache_read_input_tokens")
    cache_read_tokens = _counter(
        explicit_cache_read
        if explicit_cache_read is not None
        else _read(prompt_details, "cached_tokens"),
        default=0,
    )
    cache_creation_tokens = _counter(
        _read(usage, "cache_creation_input_tokens"), default=0
    )
    raw_reasoning = _read(completion_details, "reasoning_tokens")
    reasoning_tokens = (
        None if raw_reasoning is None else _counter(raw_reasoning)
    )
    if (
        cache_read_tokens is None
        or cache_creation_tokens is None
        or raw_reasoning is not None
        and reasoning_tokens is None
        or cache_read_tokens + cache_creation_tokens > input_tokens
        or reasoning_tokens is not None
        and reasoning_tokens > output_tokens
    ):
        return None
    return (
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens,
        reasoning_tokens,
        total_tokens,
    )


def _observation_id(
    kwargs: object,
    response_obj: object,
    *,
    provider_id: str,
    model_id: str,
    started_at: str,
    completed_at: str,
    total_tokens: int,
) -> str:
    opaque_input = _read(kwargs, "litellm_call_id") or _read(response_obj, "id")
    if not isinstance(opaque_input, str) or not opaque_input:
        opaque_input = "\x1f".join((
            provider_id,
            model_id,
            started_at,
            completed_at,
            str(total_tokens),
        ))
    digest = hashlib.sha256(opaque_input.encode("utf-8")).hexdigest()[:32]
    return f"obs_{digest}"


def build_runtime_document(
    kwargs: object,
    response_obj: object,
    start_time: object,
    end_time: object,
    *,
    scope_ref: str,
    provider_map: dict[str, str],
    status: str,
) -> dict[str, object] | None:
    """Transform allowlisted LiteLLM terminal facts into schema v1."""

    if (
        not isinstance(scope_ref, str)
        or _SCOPE_REF.fullmatch(scope_ref) is None
        or status not in _STATUSES
    ):
        return None
    identity = _provider_and_model(kwargs, provider_map)
    start = _canonical_time(start_time)
    end = _canonical_time(end_time)
    counters = _usage(response_obj)
    pricing = _cost(kwargs)
    if identity is None or start is None or end is None or counters is None or pricing is None:
        return None
    provider_id, model_id = identity
    started, started_wire = start
    completed, completed_wire = end
    if completed < started or completed - started > MAX_DURATION:
        return None
    first_token = _canonical_time(_read(kwargs, "completion_start_time"))
    if first_token is not None and not started <= first_token[0] <= completed:
        return None
    (
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens,
        reasoning_tokens,
        total_tokens,
    ) = counters
    cost_micros, cost_currency, quality = pricing
    observation_id = _observation_id(
        kwargs,
        response_obj,
        provider_id=provider_id,
        model_id=model_id,
        started_at=started_wire,
        completed_at=completed_wire,
        total_tokens=total_tokens,
    )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "observations": [{
            "observationId": observation_id,
            "providerId": provider_id,
            "modelId": model_id,
            "scopeRef": scope_ref,
            "startedAt": started_wire,
            "firstTokenAt": first_token[1] if first_token is not None else None,
            "completedAt": completed_wire,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "cacheReadTokens": cache_read_tokens,
            "cacheCreationTokens": cache_creation_tokens,
            "reasoningTokens": reasoning_tokens,
            "totalTokens": total_tokens,
            "tokenCountingConvention": "input_includes_cache",
            "status": status,
            "costMicros": cost_micros,
            "costCurrency": cost_currency,
            "sourceId": SOURCE_ID,
            "quality": quality,
        }],
    }


def _child_environment() -> dict[str, str]:
    return {
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }


class OpenUsageRuntimeLogger(CustomLogger):
    """Best-effort LiteLLM callback that writes only canonical local facts."""

    def __init__(
        self,
        *,
        collector_path: str | Path,
        database_path: str | Path,
        scope_ref: str,
        provider_map: dict[str, str],
        runner: Any = subprocess.run,
    ) -> None:
        try:
            super().__init__()
        except TypeError:
            pass
        collector = Path(collector_path)
        database = Path(database_path)
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
            or not isinstance(provider_map, dict)
            or not provider_map
            or not callable(runner)
        ):
            raise ValueError("invalid OpenUsage Runtime callback configuration")
        self._collector = collector
        self._database = database
        self._scope_ref = scope_ref
        self._provider_map = dict(provider_map)
        self._runner = runner

    def _deliver(
        self,
        kwargs: object,
        response_obj: object,
        start_time: object,
        end_time: object,
        *,
        status: str,
    ) -> bool:
        document = build_runtime_document(
            kwargs,
            response_obj,
            start_time,
            end_time,
            scope_ref=self._scope_ref,
            provider_map=self._provider_map,
            status=status,
        )
        if document is None:
            return False
        encoded = json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            return False
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
            return getattr(completed, "returncode", None) == 0
        except Exception:
            return False

    def log_success_event(
        self,
        kwargs: object,
        response_obj: object,
        start_time: object,
        end_time: object,
    ) -> bool:
        return self._deliver(
            kwargs, response_obj, start_time, end_time, status="completed"
        )

    def log_failure_event(
        self,
        kwargs: object,
        response_obj: object,
        start_time: object,
        end_time: object,
    ) -> bool:
        return self._deliver(
            kwargs, response_obj, start_time, end_time, status="error"
        )

    async def async_log_success_event(
        self,
        kwargs: object,
        response_obj: object,
        start_time: object,
        end_time: object,
    ) -> bool:
        return await asyncio.to_thread(
            self.log_success_event, kwargs, response_obj, start_time, end_time
        )

    async def async_log_failure_event(
        self,
        kwargs: object,
        response_obj: object,
        start_time: object,
        end_time: object,
    ) -> bool:
        return await asyncio.to_thread(
            self.log_failure_event, kwargs, response_obj, start_time, end_time
        )


def main(
    argv: list[str] | None = None,
    *,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    if arguments != ["--create-scope-ref"]:
        errors.write("invalid integration command\n")
        return 2
    output.write(create_scope_ref() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
