"""Provider-neutral normalization for Gateway response requests."""

from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import dataclass
from typing import Any


MAX_PROVIDER_REQUEST_BYTES = 4 * 1024 * 1024
MAX_MODEL_LENGTH = 256
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 200_000
_PROVIDER_IDS = frozenset(
    {"anthropic", "deepseek", "ollama", "openai", "openrouter"}
)
_FORBIDDEN_REQUEST_FIELDS = frozenset(
    {
        "apikey",
        "accesstoken",
        "authorization",
        "cookie",
        "credential",
        "clientsecret",
        "endpoint",
        "header",
        "headers",
        "password",
        "proxyauthorization",
        "refreshtoken",
        "secret",
        "sessioncookie",
        "token",
        "xapikey",
    }
)


@dataclass(frozen=True)
class GatewayRequest:
    """Canonical, credential-free request passed to Gateway egress."""

    provider_id: str
    model: str
    request_body: bytes
    stream: bool


def _invalid_request() -> ValueError:
    return ValueError("invalid gateway request")


def _normalized_field_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _valid_model(value: object) -> str | None:
    if type(value) is not str or not value or len(value) > MAX_MODEL_LENGTH:
        return None
    if value != value.strip():
        return None
    if any(unicodedata.category(character).startswith("C") for character in value):
        return None
    try:
        if len(value.encode("utf-8")) > MAX_MODEL_LENGTH * 4:
            return None
    except UnicodeEncodeError:
        return None
    return value


def _validate_json_value(
    value: object,
    *,
    depth: int = 0,
    counter: list[int] | None = None,
) -> None:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if depth > _MAX_JSON_DEPTH or counter[0] > _MAX_JSON_NODES:
        raise _invalid_request()

    value_type = type(value)
    if value is None or value_type in {bool, int, str}:
        return
    if value_type is float:
        if not math.isfinite(value):
            raise _invalid_request()
        return
    if value_type is list:
        for item in value:
            _validate_json_value(item, depth=depth + 1, counter=counter)
        return
    if value_type is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise _invalid_request()
            _validate_json_value(item, depth=depth + 1, counter=counter)
        return
    raise _invalid_request()


def _canonical_body(request: dict[str, Any]) -> bytes:
    _validate_json_value(request)
    try:
        encoded = json.dumps(
            request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise _invalid_request() from None
    if not encoded or len(encoded) > MAX_PROVIDER_REQUEST_BYTES:
        raise _invalid_request()
    return encoded


def parse_gateway_request(payload: object) -> GatewayRequest:
    """Validate the exact public envelope and canonicalize its native body.

    Only the top level of the provider request is checked for outbound-control
    and credential fields. Nested tool schemas and prompt text remain provider
    content; later PII middleware owns their treatment.
    """

    if type(payload) is not dict or set(payload) != {
        "provider",
        "model",
        "request",
    }:
        raise _invalid_request()

    provider_id = payload.get("provider")
    model = _valid_model(payload.get("model"))
    request = payload.get("request")
    if (
        type(provider_id) is not str
        or provider_id not in _PROVIDER_IDS
        or model is None
        or type(request) is not dict
    ):
        raise _invalid_request()

    for key in request:
        if type(key) is not str:
            raise _invalid_request()
        if _normalized_field_name(key) in _FORBIDDEN_REQUEST_FIELDS:
            raise _invalid_request()

    if _valid_model(request.get("model")) != model:
        raise _invalid_request()

    if "stream" in request:
        if type(request["stream"]) is not bool:
            raise _invalid_request()
        stream = request["stream"]
    else:
        stream = provider_id == "ollama"

    return GatewayRequest(
        provider_id=provider_id,
        model=model,
        request_body=_canonical_body(request),
        stream=stream,
    )


__all__ = [
    "GatewayRequest",
    "MAX_PROVIDER_REQUEST_BYTES",
    "parse_gateway_request",
]
