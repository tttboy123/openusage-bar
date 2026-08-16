"""Bounded clients for facts from Local API and advice from Gateway."""

from __future__ import annotations

import http.client
import json
import socket
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .contracts import (
    ADVICE_REASONS,
    API_VERSION,
    ContractError,
    MAX_BODY_BYTES,
    canonical_json,
    finite_number,
    utc_fixed6,
    validate_advice_request,
    validate_quotas_request,
    validate_usage_request,
)


MAX_INTERNAL_RESPONSE_BYTES = 1024 * 1024


class ResultTooLarge(RuntimeError):
    pass


class JSONTransport(Protocol):
    def json_request(self, method: str, path: str, body: bytes = b"") -> dict[str, object]: ...


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = str(socket_path)

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(self._socket_path)
        self.sock = connection


def _strict_object(raw: bytes) -> dict[str, object]:
    def reject(_value: str) -> None:
        raise ValueError

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8", "strict"), parse_constant=reject, object_pairs_hook=pairs)
    except Exception:
        raise RuntimeError("invalid internal response") from None
    if type(value) is not dict:
        raise RuntimeError("invalid internal response")
    return value


class BoundedJSONTransport:
    def __init__(
        self, *, host: str | None = None, port: int | None = None,
        unix_socket_path: Path | None = None, bearer_token: str | None = None,
        timeout_seconds: float = 2.0,
    ) -> None:
        tcp = host == "127.0.0.1" and type(port) is int and 1 <= port <= 65535
        unix = isinstance(unix_socket_path, Path) and unix_socket_path.is_absolute()
        if tcp == unix or not 0.1 <= timeout_seconds <= 10:
            raise ValueError("invalid internal transport")
        if bearer_token is not None and (type(bearer_token) is not str or not 1 <= len(bearer_token) <= 256):
            raise ValueError("invalid internal transport")
        self._host = host
        self._port = port
        self._socket = unix_socket_path
        self._token = bearer_token
        self._timeout = timeout_seconds

    def json_request(self, method: str, path: str, body: bytes = b"") -> dict[str, object]:
        if method not in {"GET", "POST"} or type(path) is not str or not path.startswith("/") or len(body) > MAX_BODY_BYTES:
            raise RuntimeError("invalid internal request")
        connection: http.client.HTTPConnection
        if self._socket is not None:
            connection = _UnixHTTPConnection(self._socket, self._timeout)
        else:
            connection = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        headers = {"Accept": "application/json"}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        if method == "POST":
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=body if method == "POST" else None, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_INTERNAL_RESPONSE_BYTES + 1)
            if len(raw) > MAX_INTERNAL_RESPONSE_BYTES or response.status != 200:
                raise RuntimeError("internal dependency unavailable")
            return _strict_object(raw)
        except (OSError, http.client.HTTPException, TimeoutError):
            raise RuntimeError("internal dependency unavailable") from None
        finally:
            connection.close()


def _timestamp(value: object) -> str:
    if type(value) is not str or len(value) > 64:
        raise RuntimeError("invalid Local API response")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RuntimeError("invalid Local API response") from None
    return utc_fixed6(parsed)


def _integer(value: object, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 9_007_199_254_740_991:
        raise RuntimeError("invalid Local API response")
    return value


def _text(value: object, maximum: int = 256) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum or value != value.strip():
        raise RuntimeError("invalid Local API response")
    return value


class LocalFactsClient:
    def __init__(self, transport: JSONTransport) -> None:
        if not callable(getattr(transport, "json_request", None)):
            raise ValueError("invalid Local API transport")
        self._transport = transport

    def request(self, route: str, query: dict[str, object]) -> dict[str, object]:
        if route != "/v1/health" or query != {}:
            raise RuntimeError("invalid Local API request")
        return self._transport.json_request("GET", route)

    def query_usage(self, request: dict[str, object]) -> dict[str, object]:
        validated = validate_usage_request(request)
        path = f"/v1/activity/daily?from={validated['from']}&to={validated['to']}"
        value = self._transport.json_request("GET", path)
        expected_root = {"schemaVersion", "dataRevision", "generatedAt", "rows", "coverage"}
        if type(value) is not dict or set(value) != expected_root or value["schemaVersion"] != "1.0" or type(value["rows"]) is not list:
            raise RuntimeError("invalid Local API response")
        if len(value["rows"]) > 1000:
            raise ResultTooLarge()
        rows: list[dict[str, object]] = []
        required = {
            "day", "providerId", "accountRef", "modelId", "inputTokens", "outputTokens",
            "cacheReadTokens", "cacheCreationTokens", "reasoningTokens", "totalTokens",
            "tokenCountingConvention", "costAmount", "costCurrency", "costBasis", "quality",
            "importedAt", "revision", "recordId", "sourceId", "modelFamily",
        }
        for item in value["rows"]:
            if type(item) is not dict or set(item) != required:
                raise RuntimeError("invalid Local API response")
            row: dict[str, object] = {
                "day": _text(item["day"], 10), "providerId": _text(item["providerId"], 128),
                "modelId": _text(item["modelId"]), "quality": _text(item["quality"], 64),
                "modelFamily": _text(item["modelFamily"], 128),
            }
            for name in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens", "totalTokens"):
                row[name] = _integer(item[name])
            row["reasoningTokens"] = None if item["reasoningTokens"] is None else _integer(item["reasoningTokens"])
            for name in ("costAmount", "costCurrency", "costBasis"):
                nested = item[name]
                row[name] = None if nested is None else _text(nested, 64)
            rows.append(row)
        return {
            "apiVersion": API_VERSION, "object": "plugin.usage",
            "dataRevision": _integer(value["dataRevision"]),
            "generatedAt": _timestamp(value["generatedAt"]),
            "from": validated["from"], "to": validated["to"], "rows": rows,
        }

    def query_quotas(self, request: dict[str, object]) -> dict[str, object]:
        validated = validate_quotas_request(request)
        value = self._transport.json_request("GET", f"/v1/capacity?limit={validated['limit']}")
        if type(value) is not dict or set(value) != {"schemaVersion", "dataRevision", "generatedAt", "providers"} or value["schemaVersion"] != "1.0" or type(value["providers"]) is not list or len(value["providers"]) > 128:
            raise RuntimeError("invalid Local API response")
        candidates: list[dict[str, object]] = []
        required = {"recordId", "providerId", "accountRef", "quotaName", "unit", "used", "quotaLimit", "remaining", "remainingRatio", "resetsAt", "periodStart", "periodEnd", "observedAt", "freshnessSeconds", "state", "quality", "stale", "revision", "sourceId", "quotaWindow", "appliesTo", "estimatedCostPerMillionTokens", "constraints"}
        for item in value["providers"]:
            if type(item) is not dict or set(item) != required or type(item["stale"]) is not bool:
                raise RuntimeError("invalid Local API response")
            row: dict[str, object] = {"providerId": _text(item["providerId"], 128)}
            for source, target in (("quotaName", "quotaName"), ("unit", "unit"), ("state", "state"), ("quality", "quality"), ("quotaWindow", "quotaWindow")):
                row[target] = _text(item[source], 128)
            for source, target in (("used", "used"), ("quotaLimit", "limit"), ("remaining", "remaining")):
                nested = item[source]
                row[target] = None if nested is None else _text(nested, 128)
            ratio = item["remainingRatio"]
            row["remainingRatio"] = None if ratio is None else finite_number(ratio, maximum=1.0)
            for name in ("resetsAt", "periodStart", "periodEnd"):
                nested = item[name]
                row[name] = None if nested is None else _timestamp(nested)
            row["observedAt"] = _timestamp(item["observedAt"])
            row["freshnessSeconds"] = _integer(item["freshnessSeconds"])
            row["stale"] = item["stale"]
            candidates.append(row)
        selected: dict[str, dict[str, object]] = {}
        for row in candidates:
            provider = str(row["providerId"])
            existing = selected.get(provider)
            def rank(candidate: dict[str, object]) -> tuple[int, float, str]:
                ratio = candidate["remainingRatio"]
                return (
                    1 if ratio is None else 0,
                    1.0 if ratio is None else float(ratio),
                    str(candidate["observedAt"]),
                )
            if existing is None or rank(row) < rank(existing):
                selected[provider] = row
        quotas = [selected[provider] for provider in sorted(selected)]
        if len(quotas) > 128:
            raise ResultTooLarge()
        return {"apiVersion": API_VERSION, "object": "plugin.quotas", "selection": "most_constrained_observed_scope", "dataRevision": _integer(value["dataRevision"]), "generatedAt": _timestamp(value["generatedAt"]), "quotas": quotas}


class GatewayAdviceClient:
    def __init__(self, transport: JSONTransport) -> None:
        if not callable(getattr(transport, "json_request", None)):
            raise ValueError("invalid Gateway transport")
        self._transport = transport

    def should_send(self, request: dict[str, object]) -> dict[str, object]:
        plugin_shape = {
            "apiVersion": API_VERSION, "provider": request.get("provider"),
            "model": request.get("model"), "estimatedTokens": request.get("estimated_tokens"),
            "window": request.get("window"),
        }
        validated = validate_advice_request(plugin_shape)
        body = canonical_json({
            "provider": validated["provider"], "model": validated["model"],
            "estimated_tokens": validated["estimatedTokens"], "window": validated["window"],
        })
        value = self._transport.json_request("POST", "/gateway/v1/should-send", body)
        if type(value) is not dict or set(value) != {"decision", "confidence", "reason", "defer_until", "details"} or value["decision"] not in {"yes", "no", "defer"} or value["reason"] not in ADVICE_REASONS:
            raise RuntimeError("invalid Gateway response")
        finite_number(value["confidence"], maximum=1.0)
        details = value["details"]
        if type(details) is not dict or set(details) != {"quota_remaining", "burn_rate_per_min", "predicted_exhaustion_minutes"}:
            raise RuntimeError("invalid Gateway response")
        for nested in details.values():
            if nested is not None:
                finite_number(nested)
        return json.loads(canonical_json(value))

    def health(self) -> str:
        value = self._transport.json_request("GET", "/gateway/v1/health")
        if type(value) is not dict or set(value) != {"apiVersion", "status", "mode", "capabilities"} or value["apiVersion"] != "gateway.openusage/v1":
            raise RuntimeError("invalid Gateway response")
        status = value["status"]
        if status == "disabled":
            return "disabled"
        if status == "ok":
            return "ready"
        raise RuntimeError("invalid Gateway response")
