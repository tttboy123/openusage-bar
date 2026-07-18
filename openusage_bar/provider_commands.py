from __future__ import annotations

import json
from typing import Callable, TextIO

from .config import (
    ID_PATTERN,
    DailyUsageFeedConfig,
    GenericProviderConfig,
    MiniMaxConfig,
    OpenAIOrganizationConfig,
    ProviderConfigStore,
    StepPlanConfig,
)
from .keychain import MacOSKeychain
from .network import resolve_public_addresses


MAX_REQUEST_BYTES = 131_072
CONNECTION_EDIT_FIELDS = frozenset({
    "version", "action", "providerId", "name", "apiKey", "sessionCookie",
})
MUTATION_V2_FIELDS = frozenset({
    "version", "action", "providerId", "kind", "configuration",
    "credentialMaterial",
})
MUTATION_V2_ACTIONS = frozenset({
    "create_connection", "update_connection", "remove_connection",
})
MUTATION_V2_KINDS = frozenset({
    "minimax", "step_plan", "openai_organization", "generic",
    "daily_usage_feed",
})


def _write_response(output: TextIO, ok: bool, message: str) -> int:
    json.dump(
        {"version": 1, "ok": ok, "message": message},
        output,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    output.write("\n")
    output.flush()
    return 0


def _text_field(payload: dict, name: str, limit: int, required: bool = False) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise ValueError("Provider edit request is invalid")
    if len(value.encode("utf-8")) > limit:
        raise ValueError("Provider edit request is too large")
    value = value.strip() if name in {"providerId", "name"} else value
    if required and not value:
        raise ValueError("Provider edit request is incomplete")
    return value


def _exact_object(value, fields: set[str] | frozenset[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"{label} has unsupported fields")
    return value


def _optional_text(payload: dict, name: str, limit: int = 4_096) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        raise ValueError("Provider mutation request is invalid")
    return value


def _v2_config(payload: dict):
    provider_id = _text_field(payload, "providerId", 128, required=True)
    if ID_PATTERN.fullmatch(provider_id) is None:
        raise ValueError("Provider connection identifier is invalid")
    kind = payload.get("kind")
    if kind not in MUTATION_V2_KINDS:
        raise ValueError("Provider connection type is unsupported")
    raw = payload.get("configuration")
    if not isinstance(raw, dict):
        raise ValueError("Provider configuration is invalid")
    if payload.get("action") == "remove_connection":
        _exact_object(raw, set(), "Provider configuration")
        return provider_id, kind, None

    schemas = {
        "minimax": {"name"},
        "step_plan": {"name", "site"},
        "openai_organization": {"name"},
        "generic": {
            "name", "endpoint", "headerName", "authPrefix", "primaryPath",
            "remainingPercentPath", "resetPath", "detailPath",
        },
        "daily_usage_feed": {
            "name", "familyId", "endpoint", "headerName", "authPrefix",
            "itemsPath", "datePath", "modelPath", "inputTokensPath",
            "outputTokensPath", "cacheReadTokensPath", "cacheCreationTokensPath",
            "reasoningTokensPath", "totalTokensPath", "sinceParameter",
            "untilParameter",
        },
    }
    _exact_object(raw, schemas[kind], "Provider configuration")
    name = _text_field(raw, "name", 160, required=True)
    if kind == "minimax":
        config = MiniMaxConfig(provider_id, name)
    elif kind == "step_plan":
        site = _text_field(raw, "site", 32, required=True)
        if site not in {"china", "international"}:
            raise ValueError("StepFun site is invalid")
        config = StepPlanConfig(provider_id, name, site=site)
    elif kind == "openai_organization":
        config = OpenAIOrganizationConfig(provider_id, name)
    elif kind == "generic":
        config = GenericProviderConfig(
            provider_id=provider_id, name=name,
            endpoint=_text_field(raw, "endpoint", 2_048, required=True),
            header_name=_text_field(raw, "headerName", 64, required=True),
            auth_prefix=_text_field(raw, "authPrefix", 256),
            primary_path=_text_field(raw, "primaryPath", 4_096, required=True),
            remaining_percent_path=_optional_text(raw, "remainingPercentPath"),
            reset_path=_optional_text(raw, "resetPath"),
            detail_path=_optional_text(raw, "detailPath"),
        )
    else:
        config = DailyUsageFeedConfig(
            provider_id=provider_id, name=name,
            family_id=_text_field(raw, "familyId", 128, required=True),
            endpoint=_text_field(raw, "endpoint", 2_048, required=True),
            method="GET",
            header_name=_text_field(raw, "headerName", 64, required=True),
            auth_prefix=_text_field(raw, "authPrefix", 256),
            items_path=_text_field(raw, "itemsPath", 4_096, required=True),
            date_path=_text_field(raw, "datePath", 4_096, required=True),
            model_path=_text_field(raw, "modelPath", 4_096, required=True),
            input_tokens_path=_text_field(raw, "inputTokensPath", 4_096, required=True),
            output_tokens_path=_text_field(raw, "outputTokensPath", 4_096, required=True),
            cache_read_tokens_path=_optional_text(raw, "cacheReadTokensPath"),
            cache_creation_tokens_path=_optional_text(raw, "cacheCreationTokensPath"),
            reasoning_tokens_path=_optional_text(raw, "reasoningTokensPath"),
            total_tokens_path=_text_field(raw, "totalTokensPath", 4_096, required=True),
            since_parameter=_text_field(raw, "sinceParameter", 64, required=True),
            until_parameter=_text_field(raw, "untilParameter", 64, required=True),
        )
    return provider_id, kind, config


def _v2_credentials(payload: dict, *, removing: bool) -> tuple[str, str]:
    raw = payload.get("credentialMaterial")
    fields: set[str] = set() if removing else {"primary", "session"}
    _exact_object(raw, fields, "Credential material")
    if removing:
        return "", ""
    return (
        _text_field(raw, "primary", 65_536),
        _text_field(raw, "session", 65_536),
    )


def run_provider_mutation(
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    store: ProviderConfigStore | None = None,
    keychain: MacOSKeychain | None = None,
    resolver: Callable[[str], list[str]] = resolve_public_addresses,
) -> int:
    """Apply one allowlisted provider mutation from a private stdin pipe.

    Responses are deliberately small and sanitized. Credential material is never
    copied into the response, exception text, command line, or provider config.
    """
    try:
        raw = input_stream.read(MAX_REQUEST_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("Provider edit request is too large")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Provider edit request is invalid")

        resolved_store = store or ProviderConfigStore()
        resolved_keychain = keychain or MacOSKeychain()
        if payload.get("version") == 2:
            _exact_object(payload, MUTATION_V2_FIELDS, "Provider mutation request")
            if payload.get("action") not in MUTATION_V2_ACTIONS:
                raise ValueError("Provider mutation action is unsupported")
            removing = payload["action"] == "remove_connection"
            provider_id, kind, config = _v2_config(payload)
            primary, session = _v2_credentials(payload, removing=removing)

            from .ui import ProviderController

            controller = ProviderController(
                resolved_store, resolved_keychain, resolver=resolver
            )
            if removing:
                result = controller.remove_connection(provider_id, expected_kind=kind)
            elif payload["action"] == "create_connection":
                result = controller.create_connection(config, primary, session)
            else:
                result = controller.update_connection_config(config, primary, session)
            return _write_response(output_stream, result.ok, result.message)

        if set(payload) != CONNECTION_EDIT_FIELDS:
            raise ValueError("Provider edit request has unsupported fields")
        if payload.get("version") != 1 or payload.get("action") not in {
            "update_connection", "update_step_plan",
        }:
            raise ValueError("Provider edit request is unsupported")

        provider_id = _text_field(payload, "providerId", 128, required=True)
        name = _text_field(payload, "name", 160, required=True)
        api_key = _text_field(payload, "apiKey", 65_536)
        session_cookie = _text_field(payload, "sessionCookie", 65_536)
        if ID_PATTERN.fullmatch(provider_id) is None:
            raise ValueError("Provider connection identifier is invalid")

        configs = resolved_store.load()
        existing = next(
            (item for item in configs if item.provider_id == provider_id), None
        )
        if existing is None:
            return _write_response(
                output_stream, False, "Provider connection was not found"
            )
        if (
            payload.get("action") == "update_step_plan"
            and not isinstance(existing, StepPlanConfig)
        ):
            return _write_response(
                output_stream, False, "Provider edit request is unsupported"
            )

        # Importing the AppKit-facing module is intentionally delayed until the
        # request has passed its strict schema boundary. The controller remains
        # the single owner of Keychain rollback and site-lock validation.
        from .ui import ProviderController

        result = ProviderController(resolved_store, resolved_keychain).update_connection(
            provider_id,
            name,
            api_key,
            session_cookie,
        )
        return _write_response(output_stream, result.ok, result.message)
    except (json.JSONDecodeError, UnicodeError, ValueError):
        return _write_response(output_stream, False, "Provider edit request is invalid")
    except Exception:
        return _write_response(output_stream, False, "Provider connection could not be updated")
