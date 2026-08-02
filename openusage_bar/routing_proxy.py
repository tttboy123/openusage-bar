"""Optional loopback Chat Completions execution boundary for local routing."""

from __future__ import annotations

import json
import hmac
import hashlib
import http.server
import os
import re
import secrets
import socket
import socketserver
import stat
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, TextIO

from .routing_contract import MAX_COUNTER, RouteTarget
from .routing_execution import (
    ExecutionAuthenticationError,
    ExecutionRegistry,
    ExecutionResolutionError,
    ExecutionTransientError,
)
from .routing_store import ExecutionAttemptEvidence, try_record_attempt
from .routing_targets import RouteTargetConfiguration


SCHEMA_VERSION = 1
DEFAULT_PROXY_PORT = 64123
DEFAULT_PROXY_CONFIG_PATH = (
    Path.home() / ".local" / "state" / "openusage-bar" / "routing-proxy.json"
)
MAX_PROXY_DOCUMENT_BYTES = 16 * 1024
MAX_CHAT_BODY_BYTES = 2 * 1024 * 1024
MAX_REQUEST_LINE = 8_192
MAX_ATTEMPTS = 3
DEFAULT_MAX_THREADS = 8
DEFAULT_CLIENT_TIMEOUT = 5.0
DEFAULT_REQUEST_DEADLINE = 300.0

_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEADER_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_TOP_LEVEL_KEYS = frozenset({
    "schemaVersion", "revision", "enabled", "port", "tokenDigest",
})
_LEGACY_TOP_LEVEL_KEYS = frozenset({
    "schemaVersion", "revision", "enabled", "port",
})
_TOKEN_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ProxyConfigurationError(ValueError):
    """The non-secret proxy configuration is unavailable or invalid."""


class ProxyProblem(RuntimeError):
    """A bounded, client-safe proxy failure."""

    def __init__(self, status: int, code: str):
        if (
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 400 <= status <= 599
            or not isinstance(code, str)
            or _STABLE_ID.fullmatch(code) is None
        ):
            raise ValueError("invalid proxy problem")
        super().__init__(code)
        self.status = status
        self.code = code


class ProxyStreamInterrupted(RuntimeError):
    """A sanitized terminal stream failure after downstream bytes are visible."""

    def __init__(self):
        super().__init__("proxy stream interrupted")


def _integer(value: object, *, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ProxyConfigurationError("invalid proxy configuration")
    return value


@dataclass(frozen=True)
class RoutingProxyConfiguration:
    schema_version: int
    revision: int
    enabled: bool
    port: int
    token_digest: str | None = None

    def __post_init__(self) -> None:
        if _integer(self.schema_version, minimum=1, maximum=1) != SCHEMA_VERSION:
            raise ProxyConfigurationError("invalid proxy configuration")
        _integer(self.revision, minimum=0, maximum=MAX_COUNTER)
        if not isinstance(self.enabled, bool):
            raise ProxyConfigurationError("invalid proxy configuration")
        _integer(self.port, minimum=1, maximum=65_535)
        if self.token_digest is not None and (
            not isinstance(self.token_digest, str)
            or _TOKEN_DIGEST.fullmatch(self.token_digest) is None
        ):
            raise ProxyConfigurationError("invalid proxy configuration")


class RoutingProxyConfigurationStore:
    """Read the explicit, non-secret proxy opt-in document."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> RoutingProxyConfiguration:
        if not os.path.lexists(self.path):
            return RoutingProxyConfiguration(
                SCHEMA_VERSION, 0, False, DEFAULT_PROXY_PORT
            )
        descriptor = -1
        try:
            before = self.path.lstat()
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size > MAX_PROXY_DOCUMENT_BYTES
            ):
                raise ProxyConfigurationError("invalid proxy configuration")
            descriptor = os.open(
                self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
            after = os.fstat(descriptor)
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or not stat.S_ISREG(after.st_mode)
                or after.st_uid != os.getuid()
                or stat.S_IMODE(after.st_mode) != 0o600
                or after.st_size > MAX_PROXY_DOCUMENT_BYTES
            ):
                raise ProxyConfigurationError("invalid proxy configuration")
            encoded = os.read(descriptor, after.st_size + 1)
            if len(encoded) != after.st_size:
                raise ProxyConfigurationError("invalid proxy configuration")
            payload = json.loads(
                encoded.decode("utf-8"), object_pairs_hook=_strict_config_object
            )
            if not isinstance(payload, dict) or frozenset(payload) not in {
                _TOP_LEVEL_KEYS, _LEGACY_TOP_LEVEL_KEYS,
            }:
                raise ProxyConfigurationError("invalid proxy configuration")
            return RoutingProxyConfiguration(
                schema_version=payload["schemaVersion"],
                revision=payload["revision"],
                enabled=payload["enabled"],
                port=payload["port"],
                token_digest=payload.get("tokenDigest"),
            )
        except (
            OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError
        ) as error:
            if isinstance(error, ProxyConfigurationError):
                raise
            raise ProxyConfigurationError("invalid proxy configuration") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def save(self, value: RoutingProxyConfiguration) -> None:
        if not isinstance(value, RoutingProxyConfiguration):
            raise ProxyConfigurationError("invalid proxy configuration")
        try:
            if os.path.lexists(self.path) and value.revision <= self.load().revision:
                raise ProxyConfigurationError("invalid proxy configuration")
            parent = self.path.parent
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent_stat = parent.lstat()
            if (
                not stat.S_ISDIR(parent_stat.st_mode)
                or parent_stat.st_uid != os.getuid()
                or stat.S_IMODE(parent_stat.st_mode) & 0o077
            ):
                raise ProxyConfigurationError("invalid proxy configuration")
            payload = {
                "schemaVersion": value.schema_version,
                "revision": value.revision,
                "enabled": value.enabled,
                "port": value.port,
                "tokenDigest": value.token_digest,
            }
            encoded = (
                json.dumps(payload, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            descriptor, temporary = tempfile.mkstemp(
                prefix="routing-proxy.", suffix=".json", dir=parent
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = -1
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                os.chmod(self.path, 0o600)
                directory = os.open(
                    parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if os.path.lexists(temporary):
                    os.unlink(temporary)
        except (OSError, TypeError, ValueError) as error:
            if isinstance(error, ProxyConfigurationError):
                raise
            raise ProxyConfigurationError("invalid proxy configuration") from error


def _strict_config_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProxyConfigurationError("invalid proxy configuration")
        value[key] = item
    return value


@dataclass(frozen=True)
class ProxyCompletion:
    status: int
    headers: dict[str, str]
    body: dict[str, object]


@dataclass(frozen=True)
class PreparedProxyStream:
    status: int
    headers: dict[str, str]
    model_id: str
    chunks: Iterator[bytes]


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("proxy clock is invalid")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _bounded_json(value: object) -> int:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ProxyProblem(400, "invalid_request") from error
    if len(encoded) > MAX_CHAT_BODY_BYTES:
        raise ProxyProblem(413, "request_too_large")
    return len(encoded)


def _output_limit(body: dict[str, object]) -> int:
    values = [
        body.get("max_completion_tokens"),
        body.get("max_tokens"),
    ]
    present = [value for value in values if value is not None]
    if not present:
        return 4_096
    value = present[0]
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_COUNTER
    ):
        raise ProxyProblem(400, "invalid_request")
    return value


def _capabilities(body: dict[str, object]) -> list[str]:
    values = ["chat"]
    if body.get("tools") is not None:
        values.append("tools")
    if body.get("reasoning_effort") is not None:
        values.append("reasoning")
    return values


def _decision_request(
    body: dict[str, object],
    model: str,
    encoded_size: int,
    default_policy_id: str,
) -> dict[str, object]:
    if model.startswith("openusage/"):
        selected_policy = model.removeprefix("openusage/")
        policy_id = (
            default_policy_id if selected_policy == "auto" else selected_policy
        )
        if _STABLE_ID.fullmatch(policy_id) is None:
            raise ProxyProblem(400, "invalid_model")
        allowed_targets: list[str] = []
    else:
        if _STABLE_ID.fullmatch(model) is None:
            raise ProxyProblem(400, "invalid_model")
        policy_id = "reliable"
        allowed_targets = [model]
    output_limit = _output_limit(body)
    input_estimate = max(1, (encoded_size + 3) // 4)
    context = min(MAX_COUNTER, input_estimate + output_limit)
    return {
        "schemaVersion": "1.0",
        "policyId": policy_id,
        "task": {
            "kind": "chat",
            "requiredCapabilities": _capabilities(body),
            "estimatedInputTokens": input_estimate,
            "maxOutputTokens": output_limit,
            "minimumContextWindowTokens": context,
            "privacy": "allow_proxy",
            "regions": ["global"],
        },
        "constraints": {
            "allowProviders": [],
            "denyProviders": [],
            "allowTargets": allowed_targets,
            "denyTargets": [],
            "maximumEstimatedCostMicrounits": None,
            "costCurrency": None,
        },
        "session": None,
    }


def _usage(response: dict[str, object]) -> dict[str, int] | None:
    raw = response.get("usage")
    if not isinstance(raw, dict):
        return None
    prompt = raw.get("prompt_tokens")
    completion = raw.get("completion_tokens")
    total = raw.get("total_tokens")
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_COUNTER
        for value in (prompt, completion, total)
    ):
        return None
    cached = 0
    prompt_details = raw.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        candidate = prompt_details.get("cached_tokens", 0)
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            cached = min(candidate, prompt)
    reasoning = None
    completion_details = raw.get("completion_tokens_details")
    if isinstance(completion_details, dict):
        candidate = completion_details.get("reasoning_tokens")
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            reasoning = min(candidate, completion)
    return {
        "input": prompt,
        "output": completion,
        "cache_read": cached,
        "cache_creation": 0,
        "reasoning": reasoning,
        "total": total,
    }


class RoutingProxyController:
    """Resolve a fresh Phase A decision and execute at most three targets."""

    def __init__(
        self,
        *,
        decision_controller: object,
        target_loader: Callable[[], RouteTargetConfiguration],
        registry_loader: Callable[[], ExecutionRegistry],
        evidence_store: object,
        default_policy_loader: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.decision_controller = decision_controller
        self.target_loader = target_loader
        self.registry_loader = registry_loader
        self.evidence_store = evidence_store
        self.default_policy_loader = default_policy_loader or (lambda: "reliable")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _targets(self) -> dict[str, RouteTarget]:
        configuration = self.target_loader()
        if not isinstance(configuration, RouteTargetConfiguration):
            raise ProxyProblem(503, "routing_unavailable")
        return {value.target_id: value for value in configuration.targets}

    def models(self) -> dict[str, object]:
        registry = self.registry_loader()
        values: list[dict[str, object]] = []
        for target in sorted(self._targets().values(), key=lambda item: item.target_id):
            try:
                registry.resolve(target)
            except Exception:
                continue
            values.append({
                "id": target.target_id,
                "object": "model",
                "created": 0,
                "owned_by": "openusage",
            })
        values.append({
            "id": "openusage/auto",
            "object": "model",
            "created": 0,
            "owned_by": "openusage",
        })
        values.append({
            "id": "openusage/reliable",
            "object": "model",
            "created": 0,
            "owned_by": "openusage",
        })
        return {
            "object": "list",
            "data": sorted(values, key=lambda value: str(value["id"])),
        }

    def _record(
        self,
        *,
        decision_id: str,
        target: RouteTarget,
        ordinal: int,
        started: datetime,
        outcome: str,
        status_class: str,
        reason_code: str,
        response: dict[str, object] | None = None,
    ) -> None:
        completed = self.clock()
        tokens = None if response is None else _usage(response)
        evidence = ExecutionAttemptEvidence(
            attempt_id="attempt_" + secrets.token_hex(16),
            decision_id=decision_id,
            target_id=target.target_id,
            ordinal=ordinal,
            started_at=_timestamp(started),
            completed_at=_timestamp(completed),
            outcome=outcome,
            status_class=status_class,
            reason_code=reason_code,
            input_tokens=None if tokens is None else tokens["input"],
            output_tokens=None if tokens is None else tokens["output"],
            cache_read_tokens=None if tokens is None else tokens["cache_read"],
            cache_creation_tokens=None if tokens is None else tokens["cache_creation"],
            reasoning_tokens=None if tokens is None else tokens["reasoning"],
            total_tokens=None if tokens is None else tokens["total"],
            cost_micros=None,
            cost_currency=None,
        )
        try_record_attempt(self.evidence_store, evidence)

    def _decision_candidates(
        self, payload: object, *, stream: bool
    ) -> tuple[
        dict[str, object], str, tuple[RouteTarget, ...], ExecutionRegistry
    ]:
        if not isinstance(payload, dict):
            raise ProxyProblem(400, "invalid_request")
        body = dict(payload)
        encoded_size = _bounded_json(body)
        model = body.get("model")
        if not isinstance(model, str) or not model:
            raise ProxyProblem(400, "invalid_model")
        if body.get("stream", False) is not stream:
            raise ProxyProblem(
                400, "invalid_request" if stream else "stream_not_supported"
            )
        try:
            default_policy_id = self.default_policy_loader()
        except Exception as error:
            raise ProxyProblem(503, "routing_unavailable") from error
        if (
            not isinstance(default_policy_id, str)
            or _STABLE_ID.fullmatch(default_policy_id) is None
        ):
            raise ProxyProblem(503, "routing_unavailable")
        request = _decision_request(
            body, model, encoded_size, default_policy_id
        )
        try:
            decision = self.decision_controller.decide(request, simulated=False)
        except Exception as error:
            if isinstance(error, ProxyProblem):
                raise
            raise ProxyProblem(503, "routing_unavailable") from error
        if not isinstance(decision, dict):
            raise ProxyProblem(503, "routing_unavailable")
        decision_id = decision.get("decisionId")
        expires_at = decision.get("expiresAt")
        selected = decision.get("selected")
        alternatives = decision.get("alternatives")
        if (
            not isinstance(decision_id, str)
            or not isinstance(expires_at, str)
            or not expires_at.endswith("Z")
            or not isinstance(selected, dict)
            or not isinstance(alternatives, list)
        ):
            raise ProxyProblem(503, "no_route")
        try:
            expiry = datetime.fromisoformat(
                expires_at[:-1] + "+00:00"
            ).astimezone(timezone.utc)
        except ValueError as error:
            raise ProxyProblem(503, "routing_unavailable") from error
        current = self.clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ProxyProblem(503, "routing_unavailable")
        if expiry <= current.astimezone(timezone.utc):
            raise ProxyProblem(503, "decision_expired")
        target_ids = [selected.get("targetId")] + [
            value.get("targetId")
            for value in alternatives
            if isinstance(value, dict)
        ]
        indexed = self._targets()
        candidates: list[RouteTarget] = []
        for target_id in target_ids[:MAX_ATTEMPTS]:
            target = indexed.get(target_id) if isinstance(target_id, str) else None
            if target is None:
                raise ProxyProblem(503, "routing_unavailable")
            candidates.append(target)
        if not candidates:
            raise ProxyProblem(503, "no_route")
        return body, decision_id, tuple(candidates), self.registry_loader()

    def complete(self, payload: object) -> ProxyCompletion:
        body, decision_id, candidates, registry = self._decision_candidates(
            payload, stream=False
        )
        for ordinal, target in enumerate(candidates, start=1):
            started = self.clock()
            try:
                adapter, execution_connection = registry.resolve(target)
                response = adapter.execute_chat(
                    execution_connection, target.model_id, body
                )
            except ExecutionTransientError as error:
                self._record(
                    decision_id=decision_id, target=target, ordinal=ordinal,
                    started=started, outcome="transient_failure",
                    status_class=error.status_class,
                    reason_code=error.reason_code,
                )
                if ordinal < len(candidates):
                    continue
                raise ProxyProblem(503, "upstream_temporarily_unavailable") from error
            except ExecutionAuthenticationError as error:
                self._record(
                    decision_id=decision_id, target=target, ordinal=ordinal,
                    started=started, outcome="permanent_failure",
                    status_class="authentication", reason_code="authentication_failed",
                )
                raise ProxyProblem(502, "upstream_authentication_failed") from error
            except ExecutionResolutionError as error:
                self._record(
                    decision_id=decision_id, target=target, ordinal=ordinal,
                    started=started, outcome="permanent_failure",
                    status_class="capability", reason_code="capability_error",
                )
                raise ProxyProblem(503, "execution_target_unavailable") from error
            except (TypeError, ValueError) as error:
                self._record(
                    decision_id=decision_id, target=target, ordinal=ordinal,
                    started=started, outcome="permanent_failure",
                    status_class="invalid_request", reason_code="invalid_request",
                )
                raise ProxyProblem(400, "invalid_request") from error
            except Exception as error:
                self._record(
                    decision_id=decision_id, target=target, ordinal=ordinal,
                    started=started, outcome="permanent_failure",
                    status_class="unknown", reason_code="provider_unavailable",
                )
                raise ProxyProblem(502, "upstream_failure") from error
            if not isinstance(response, dict):
                raise ProxyProblem(502, "invalid_upstream_response")
            public = dict(response)
            public["model"] = target.model_id
            self._record(
                decision_id=decision_id, target=target, ordinal=ordinal,
                started=started, outcome="succeeded", status_class="success",
                reason_code="provider_completed", response=public,
            )
            return ProxyCompletion(
                status=200,
                headers={"x-openusage-route-id": decision_id},
                body=public,
            )
        raise ProxyProblem(503, "no_route")

    def prepare_stream(self, payload: object) -> PreparedProxyStream:
        body, decision_id, candidates, registry = self._decision_candidates(
            payload, stream=True
        )
        for ordinal, target in enumerate(candidates, start=1):
            started = self.clock()
            try:
                adapter, execution_connection = registry.resolve(target)
                execute = getattr(adapter, "execute_chat_stream", None)
                if not callable(execute):
                    raise ExecutionResolutionError(
                        "streaming execution target is unavailable"
                    )
                upstream = iter(execute(
                    execution_connection, target.model_id, body
                ))
                first = next(upstream)
                if not isinstance(first, bytes) or not first:
                    raise ValueError("invalid chat execution stream")
            except StopIteration as error:
                self._record(
                    decision_id=decision_id, target=target, ordinal=ordinal,
                    started=started, outcome="permanent_failure",
                    status_class="unknown", reason_code="provider_unavailable",
                )
                raise ProxyProblem(502, "invalid_upstream_response") from error
            except ExecutionTransientError as error:
                self._record(
                    decision_id=decision_id, target=target, ordinal=ordinal,
                    started=started, outcome="transient_failure",
                    status_class=error.status_class,
                    reason_code=error.reason_code,
                )
                if ordinal < len(candidates):
                    continue
                raise ProxyProblem(503, "upstream_temporarily_unavailable") from error
            except ExecutionAuthenticationError as error:
                self._record(
                    decision_id=decision_id, target=target, ordinal=ordinal,
                    started=started, outcome="permanent_failure",
                    status_class="authentication", reason_code="authentication_failed",
                )
                raise ProxyProblem(502, "upstream_authentication_failed") from error
            except ExecutionResolutionError as error:
                self._record(
                    decision_id=decision_id, target=target, ordinal=ordinal,
                    started=started, outcome="permanent_failure",
                    status_class="capability", reason_code="capability_error",
                )
                raise ProxyProblem(503, "execution_target_unavailable") from error
            except (TypeError, ValueError) as error:
                self._record(
                    decision_id=decision_id, target=target, ordinal=ordinal,
                    started=started, outcome="permanent_failure",
                    status_class="invalid_request", reason_code="invalid_request",
                )
                raise ProxyProblem(400, "invalid_request") from error
            except Exception as error:
                self._record(
                    decision_id=decision_id, target=target, ordinal=ordinal,
                    started=started, outcome="permanent_failure",
                    status_class="unknown", reason_code="provider_unavailable",
                )
                raise ProxyProblem(502, "upstream_failure") from error

            def locked_chunks(
                first_chunk: bytes = first,
                remaining: Iterator[bytes] = upstream,
                selected: RouteTarget = target,
                selected_ordinal: int = ordinal,
                selected_started: datetime = started,
            ) -> Iterator[bytes]:
                terminal_recorded = False
                try:
                    yield first_chunk
                    for chunk in remaining:
                        if not isinstance(chunk, bytes) or not chunk:
                            raise ValueError("invalid chat execution stream")
                        yield chunk
                    self._record(
                        decision_id=decision_id, target=selected,
                        ordinal=selected_ordinal, started=selected_started,
                        outcome="succeeded", status_class="success",
                        reason_code="provider_completed",
                    )
                    terminal_recorded = True
                except ExecutionTransientError as error:
                    self._record(
                        decision_id=decision_id, target=selected,
                        ordinal=selected_ordinal, started=selected_started,
                        outcome="transient_failure",
                        status_class=error.status_class,
                        reason_code="stream_started",
                    )
                    terminal_recorded = True
                    raise ProxyStreamInterrupted() from error
                except GeneratorExit:
                    self._record(
                        decision_id=decision_id, target=selected,
                        ordinal=selected_ordinal, started=selected_started,
                        outcome="cancelled", status_class="client_disconnect",
                        reason_code="client_disconnected",
                    )
                    terminal_recorded = True
                    raise
                except Exception as error:
                    self._record(
                        decision_id=decision_id, target=selected,
                        ordinal=selected_ordinal, started=selected_started,
                        outcome="permanent_failure", status_class="unknown",
                        reason_code="stream_started",
                    )
                    terminal_recorded = True
                    raise ProxyStreamInterrupted() from error
                finally:
                    close = getattr(remaining, "close", None)
                    if callable(close):
                        close()
                    if not terminal_recorded:
                        self._record(
                            decision_id=decision_id, target=selected,
                            ordinal=selected_ordinal, started=selected_started,
                            outcome="cancelled", status_class="client_disconnect",
                            reason_code="client_disconnected",
                        )

            return PreparedProxyStream(
                status=200,
                headers={"x-openusage-route-id": decision_id},
                model_id=target.model_id,
                chunks=locked_chunks(),
            )
        raise ProxyProblem(503, "no_route")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


class _BoundedProxyThreads:
    daemon_threads = True
    block_on_close = False

    def _configure_threads(self, maximum: int, timeout: float, deadline: float) -> None:
        self._thread_slots = threading.BoundedSemaphore(maximum)
        self._client_timeout = timeout
        self._request_deadline = deadline
        self._deadline_lock = threading.Lock()
        self._deadline_timers: dict[socket.socket, threading.Timer] = {}
        self._closing = False

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(self._client_timeout)
        return request, address

    def handle_error(self, request: socket.socket, client_address: Any) -> None:
        return

    def process_request(self, request, client_address) -> None:
        if not self._thread_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._thread_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        timer = threading.Timer(
            self._request_deadline,
            self._expire_request,
            args=(request,),
        )
        timer.daemon = True
        with self._deadline_lock:
            if self._closing:
                reject = True
            else:
                self._deadline_timers[request] = timer
                timer.start()
                reject = False
        if reject:
            self.shutdown_request(request)
            self._thread_slots.release()
            return
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()
            if timer is not threading.current_thread():
                timer.join()
            with self._deadline_lock:
                self._deadline_timers.pop(request, None)
            self._thread_slots.release()

    @staticmethod
    def _expire_request(request: socket.socket) -> None:
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    @property
    def active_deadline_count(self) -> int:
        with self._deadline_lock:
            return len(self._deadline_timers)

    def _abort_active_requests(self) -> None:
        with self._deadline_lock:
            self._closing = True
            active = tuple(self._deadline_timers.items())
        for request, timer in active:
            timer.cancel()
            self._expire_request(request)
        for _, timer in active:
            if timer is not threading.current_thread():
                timer.join()
        with self._deadline_lock:
            for request, timer in active:
                if self._deadline_timers.get(request) is timer:
                    self._deadline_timers.pop(request, None)

    def server_close(self) -> None:
        self._abort_active_requests()
        super().server_close()


class _RoutingProxyHTTPServer(
    _BoundedProxyThreads,
    socketserver.ThreadingMixIn,
    http.server.HTTPServer,
):
    allow_reuse_address = False
    request_queue_size = DEFAULT_MAX_THREADS

    def __init__(
        self,
        controller: RoutingProxyController,
        bearer_token_digest: str,
        port: int,
        *,
        max_threads: int,
        client_timeout: float,
        request_deadline: float,
    ) -> None:
        self.controller = controller
        self._bearer_lock = threading.Lock()
        self.bearer_token_digest = bearer_token_digest
        super().__init__(("127.0.0.1", port), _RoutingProxyHandler)
        self._configure_threads(max_threads, client_timeout, request_deadline)

    def replace_bearer_token_digest(self, value: str) -> None:
        if not isinstance(value, str) or _TOKEN_DIGEST.fullmatch(value) is None:
            raise ValueError("proxy bearer token is invalid")
        with self._bearer_lock:
            self.bearer_token_digest = value

    def authorizes(self, candidate: str) -> bool:
        candidate_digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        with self._bearer_lock:
            expected = self.bearer_token_digest
        return hmac.compare_digest(expected, candidate_digest)


class RoutingProxySupervisor:
    """Apply private proxy configuration revisions without restarting Collector."""

    def __init__(
        self,
        *,
        configuration_store: RoutingProxyConfigurationStore,
        controller: RoutingProxyController,
        server_factory: Callable[..., object],
        on_error: Callable[[], None],
        poll_seconds: float = 0.5,
    ) -> None:
        if (
            not isinstance(configuration_store, RoutingProxyConfigurationStore)
            or not isinstance(controller, RoutingProxyController)
            or not callable(server_factory)
            or not callable(on_error)
            or isinstance(poll_seconds, bool)
            or not isinstance(poll_seconds, (int, float))
            or not 0.1 <= poll_seconds <= 60
        ):
            raise ValueError("proxy supervisor is invalid")
        self.configuration_store = configuration_store
        self.controller = controller
        self.server_factory = server_factory
        self.on_error = on_error
        self.poll_seconds = float(poll_seconds)
        self._seen_revision: int | None = None
        self._active_configuration: RoutingProxyConfiguration | None = None
        self._active_token_digest: str | None = None
        self._server: object | None = None
        self._thread: threading.Thread | None = None
        self._reported_invalid_configuration = False
        self._reported_runtime_revision: int | None = None

    def _report_runtime_error(self, revision: int) -> None:
        if self._reported_runtime_revision != revision:
            self.on_error()
            self._reported_runtime_revision = revision

    def _new_runtime(
        self, configuration: RoutingProxyConfiguration, token_digest: str
    ) -> tuple[object, threading.Thread]:
        server = self.server_factory(
            self.controller,
            bearer_token_digest=token_digest,
            port=configuration.port,
        )
        serve = getattr(server, "serve_forever", None)
        if not callable(serve):
            raise ValueError("proxy server is invalid")
        thread = threading.Thread(
            target=serve,
            name="openusage-routing-proxy",
            daemon=True,
        )
        thread.start()
        return server, thread

    @staticmethod
    def _stop_runtime(
        server: object | None, thread: threading.Thread | None
    ) -> None:
        if server is None:
            return
        shutdown = getattr(server, "shutdown", None)
        close = getattr(server, "server_close", None)
        try:
            if callable(shutdown):
                shutdown()
        except Exception:
            pass
        try:
            if callable(close):
                close()
        except Exception:
            pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(5)

    def _clear_active(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        self._active_configuration = None
        self._active_token_digest = None
        self._stop_runtime(server, thread)

    def reconcile(self) -> None:
        try:
            configuration = self.configuration_store.load()
        except Exception:
            if not self._reported_invalid_configuration:
                self.on_error()
                self._reported_invalid_configuration = True
            return
        self._reported_invalid_configuration = False
        if configuration.revision == self._seen_revision:
            return
        if not configuration.enabled:
            self._clear_active()
            self._seen_revision = configuration.revision
            self._reported_runtime_revision = None
            return
        token_digest = configuration.token_digest
        if token_digest is None:
            self._report_runtime_error(configuration.revision)
            return

        old_server = self._server
        old_thread = self._thread
        old_configuration = self._active_configuration
        old_token_digest = self._active_token_digest
        same_port = (
            old_configuration is not None
            and old_configuration.port == configuration.port
        )
        if same_port:
            replace_digest = getattr(
                old_server, "replace_bearer_token_digest", None
            )
            if callable(replace_digest):
                try:
                    replace_digest(token_digest)
                except Exception:
                    self._report_runtime_error(configuration.revision)
                    return
                self._active_configuration = configuration
                self._active_token_digest = token_digest
                self._seen_revision = configuration.revision
                self._reported_runtime_revision = None
                return
        if same_port:
            self._clear_active()
        try:
            server, thread = self._new_runtime(configuration, token_digest)
        except Exception:
            self._report_runtime_error(configuration.revision)
            if (
                same_port
                and old_configuration is not None
                and old_token_digest is not None
            ):
                try:
                    self._server, self._thread = self._new_runtime(
                        old_configuration, old_token_digest
                    )
                    self._active_configuration = old_configuration
                    self._active_token_digest = old_token_digest
                except Exception:
                    self._server = None
                    self._thread = None
            return
        self._server = server
        self._thread = thread
        self._active_configuration = configuration
        self._active_token_digest = token_digest
        self._seen_revision = configuration.revision
        self._reported_runtime_revision = None
        if not same_port:
            self._stop_runtime(old_server, old_thread)

    def run(self, stop_event: threading.Event) -> None:
        if not isinstance(stop_event, threading.Event):
            raise ValueError("proxy stop event is invalid")
        while not stop_event.is_set():
            self.reconcile()
            stop_event.wait(self.poll_seconds)

    def close(self) -> None:
        self._clear_active()


class _RoutingProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OpenUsageProxy/1"
    sys_version = ""

    @property
    def proxy_server(self) -> _RoutingProxyHTTPServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: object) -> None:
        return

    def handle_one_request(self) -> None:
        try:
            self.raw_requestline = self.rfile.readline(MAX_REQUEST_LINE + 1)
            if len(self.raw_requestline) > MAX_REQUEST_LINE:
                self.requestline = ""
                self.request_version = "HTTP/1.1"
                self.command = ""
                self._problem(ProxyProblem(413, "request_too_large"))
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            if self.request_version != "HTTP/1.1":
                self.request_version = "HTTP/1.1"
                self._problem(ProxyProblem(400, "invalid_request"))
                return
            method = getattr(self, "do_" + self.command, None)
            if method is None:
                self._problem(ProxyProblem(405, "method_not_allowed"))
                return
            method()
            self.wfile.flush()
        except TimeoutError:
            self.close_connection = True

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Replace parser HTML and details with one stable JSON envelope."""
        del message, explain
        status = 413 if code in {414, 431} else 400
        self.request_version = "HTTP/1.1"
        self.close_connection = True
        self._problem(
            ProxyProblem(
                status,
                "request_too_large" if status == 413 else "invalid_request",
            )
        )

    def _validate_request(self) -> None:
        """Reject ambiguous HTTP framing before authentication or dispatch."""
        if self.request_version != "HTTP/1.1":
            raise ProxyProblem(400, "invalid_request")
        if not self.path.startswith("/") or self.path.startswith("//"):
            raise ProxyProblem(400, "invalid_request")

        for name, value in self.headers.raw_items():
            if (
                not name
                or _HEADER_CONTROL.search(name) is not None
                or _HEADER_CONTROL.search(value) is not None
            ):
                raise ProxyProblem(400, "invalid_request")

        hosts = self.headers.get_all("Host", [])
        expected_host = f"127.0.0.1:{self.proxy_server.server_port}"
        if len(hosts) != 1 or not hmac.compare_digest(hosts[0], expected_host):
            raise ProxyProblem(400, "invalid_request")
        if len(self.headers.get_all("Authorization", [])) > 1:
            raise ProxyProblem(400, "invalid_request")
        if len(self.headers.get_all("Content-Length", [])) > 1:
            raise ProxyProblem(400, "invalid_request")
        if self.headers.get_all("Transfer-Encoding", []):
            raise ProxyProblem(400, "invalid_request")
        if self.headers.get_all("Origin", []):
            raise ProxyProblem(400, "invalid_request")

    def _authorized(self) -> bool:
        values = self.headers.get_all("Authorization", [])
        if len(values) != 1 or not values[0].startswith("Bearer "):
            return False
        candidate = values[0][len("Bearer "):]
        return self.proxy_server.authorizes(candidate)

    def _write_json(
        self,
        status: int,
        payload: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        encoded = json.dumps(
            payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)
        self.close_connection = True

    def _problem(self, value: ProxyProblem) -> None:
        headers = (
            {"WWW-Authenticate": "Bearer"}
            if value.status == 401
            else None
        )
        self._write_json(
            value.status,
            {
                "error": {
                    "message": value.code,
                    "type": "openusage_proxy_error",
                    "param": None,
                    "code": value.code,
                }
            },
            headers=headers,
        )

    def _require_authorization(self) -> None:
        if not self._authorized():
            raise ProxyProblem(401, "unauthorized")

    def do_GET(self) -> None:
        try:
            self._validate_request()
            self._require_authorization()
            if self.path != "/v1/models":
                raise ProxyProblem(404, "not_found")
            self._write_json(200, self.proxy_server.controller.models())
        except ProxyProblem as error:
            self._problem(error)
        except Exception:
            self._problem(ProxyProblem(503, "proxy_unavailable"))

    def _read_json_body(self) -> dict[str, object]:
        if self.headers.get_all("Transfer-Encoding", []):
            raise ProxyProblem(400, "invalid_request")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise ProxyProblem(411, "content_length_required")
        length = int(lengths[0])
        if not 1 <= length <= MAX_CHAT_BODY_BYTES:
            raise ProxyProblem(
                413 if length > MAX_CHAT_BODY_BYTES else 400,
                "request_too_large" if length > MAX_CHAT_BODY_BYTES else "invalid_request",
            )
        content_types = self.headers.get_all("Content-Type", [])
        if (
            len(content_types) != 1
            or content_types[0].split(";", 1)[0].strip().casefold()
            != "application/json"
        ):
            raise ProxyProblem(415, "unsupported_media_type")
        encoded = self.rfile.read(length)
        if len(encoded) != length:
            raise ProxyProblem(400, "invalid_request")
        try:
            payload = json.loads(
                encoded.decode("utf-8"), object_pairs_hook=_strict_json_object
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise ProxyProblem(400, "invalid_request") from error
        if not isinstance(payload, dict):
            raise ProxyProblem(400, "invalid_request")
        return payload

    def do_POST(self) -> None:
        try:
            self._validate_request()
            self._require_authorization()
            if self.path != "/v1/chat/completions":
                raise ProxyProblem(404, "not_found")
            body = self._read_json_body()
            if body.get("stream", False) is True:
                stream = self.proxy_server.controller.prepare_stream(body)
                self.send_response(stream.status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.send_header("X-OpenUsage-Model", stream.model_id)
                for key, value in stream.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    for chunk in stream.chunks:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionError, OSError, ProxyStreamInterrupted):
                    close = getattr(stream.chunks, "close", None)
                    if callable(close):
                        close()
                finally:
                    self.close_connection = True
                return
            result = self.proxy_server.controller.complete(body)
            self._write_json(
                result.status, result.body, headers=result.headers
            )
        except ProxyProblem as error:
            self._problem(error)
        except Exception:
            self._problem(ProxyProblem(503, "proxy_unavailable"))

    def do_HEAD(self) -> None:
        self._problem(ProxyProblem(405, "method_not_allowed"))

    def do_PUT(self) -> None:
        self._problem(ProxyProblem(405, "method_not_allowed"))

    def do_DELETE(self) -> None:
        self._problem(ProxyProblem(405, "method_not_allowed"))

    def do_PATCH(self) -> None:
        self._problem(ProxyProblem(405, "method_not_allowed"))


def create_routing_proxy_server(
    controller: RoutingProxyController,
    *,
    bearer_token: str | None = None,
    bearer_token_digest: str | None = None,
    port: int = DEFAULT_PROXY_PORT,
    max_threads: int = DEFAULT_MAX_THREADS,
    client_timeout: float = DEFAULT_CLIENT_TIMEOUT,
    request_deadline: float = DEFAULT_REQUEST_DEADLINE,
) -> _RoutingProxyHTTPServer:
    """Create an explicitly enabled IPv4-loopback-only proxy server."""
    if not isinstance(controller, RoutingProxyController):
        raise ValueError("proxy controller is invalid")
    if (bearer_token is None) == (bearer_token_digest is None):
        raise ValueError("proxy bearer token is invalid")
    if bearer_token is not None:
        if (
            not isinstance(bearer_token, str)
            or not 24 <= len(bearer_token.encode("utf-8")) <= 256
            or any(character.isspace() for character in bearer_token)
        ):
            raise ValueError("proxy bearer token is invalid")
        resolved_digest = hashlib.sha256(
            bearer_token.encode("utf-8")
        ).hexdigest()
    elif (
        not isinstance(bearer_token_digest, str)
        or _TOKEN_DIGEST.fullmatch(bearer_token_digest) is None
    ):
        raise ValueError("proxy bearer token is invalid")
    else:
        resolved_digest = bearer_token_digest
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 0 <= port <= 65_535
    ):
        raise ValueError("proxy port is invalid")
    if (
        isinstance(max_threads, bool)
        or not isinstance(max_threads, int)
        or not 1 <= max_threads <= 256
    ):
        raise ValueError("proxy max_threads is invalid")
    if (
        isinstance(client_timeout, bool)
        or not isinstance(client_timeout, (int, float))
        or not 0.1 <= client_timeout <= 60
    ):
        raise ValueError("proxy client_timeout is invalid")
    if (
        isinstance(request_deadline, bool)
        or not isinstance(request_deadline, (int, float))
        or not 0.05 <= request_deadline <= 300
    ):
        raise ValueError("proxy request_deadline is invalid")
    return _RoutingProxyHTTPServer(
        controller,
        resolved_digest,
        port,
        max_threads=max_threads,
        client_timeout=float(client_timeout),
        request_deadline=float(request_deadline),
    )


def _proxy_status(
    value: RoutingProxyConfiguration, *, include_restart: bool
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schemaVersion": "1.0",
        "enabled": value.enabled,
        "endpoint": f"http://127.0.0.1:{value.port}/v1",
        "configurationRevision": value.revision,
    }
    if include_restart:
        payload["restartRequired"] = False
    return payload


def run_proxy_command(
    args: object,
    *,
    stdout: TextIO,
    stderr: TextIO,
    config_store: RoutingProxyConfigurationStore | None = None,
    token_factory: Callable[[], str] | None = None,
) -> int:
    """Manage proxy opt-in without persisting the raw bearer token."""

    store = config_store or RoutingProxyConfigurationStore(
        DEFAULT_PROXY_CONFIG_PATH
    )
    try:
        current = store.load()
        command = getattr(args, "proxy_command", None)
        include_restart = False
        bearer_token: str | None = None
        if command == "status":
            updated = current
        elif command == "enable":
            port = getattr(args, "port", None)
            if port is None:
                port = current.port
            _integer(port, minimum=1, maximum=65_535)
            generated = (token_factory or (lambda: secrets.token_urlsafe(32)))()
            if (
                not isinstance(generated, str)
                or not 24 <= len(generated.encode("utf-8")) <= 256
                or any(character.isspace() for character in generated)
            ):
                raise ValueError("invalid generated bearer token")
            updated = RoutingProxyConfiguration(
                SCHEMA_VERSION,
                current.revision + 1,
                True,
                port,
                hashlib.sha256(generated.encode("utf-8")).hexdigest(),
            )
            store.save(updated)
            bearer_token = generated
            include_restart = True
        elif command == "disable":
            updated = RoutingProxyConfiguration(
                SCHEMA_VERSION,
                current.revision + 1,
                False,
                current.port,
                current.token_digest,
            )
            store.save(updated)
            include_restart = True
        elif command == "rotate":
            if not current.enabled:
                raise ValueError("proxy is disabled")
            generated = (token_factory or (lambda: secrets.token_urlsafe(32)))()
            if (
                not isinstance(generated, str)
                or not 24 <= len(generated.encode("utf-8")) <= 256
                or any(character.isspace() for character in generated)
            ):
                raise ValueError("invalid generated bearer token")
            updated = RoutingProxyConfiguration(
                SCHEMA_VERSION,
                current.revision + 1,
                True,
                current.port,
                hashlib.sha256(generated.encode("utf-8")).hexdigest(),
            )
            store.save(updated)
            bearer_token = generated
            include_restart = True
        else:
            raise ValueError("invalid proxy command")
        payload = _proxy_status(updated, include_restart=include_restart)
        if bearer_token is not None:
            payload["bearerToken"] = bearer_token
        stdout.write(
            json.dumps(
                payload, ensure_ascii=True, allow_nan=False,
                sort_keys=True, separators=(",", ":"),
            ) + "\n"
        )
        return 0
    except (ProxyConfigurationError, TypeError, ValueError):
        stderr.write("invalid proxy input\n")
        return 2
    except Exception:
        stderr.write("proxy configuration unavailable\n")
        return 1
