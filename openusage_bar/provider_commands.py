from __future__ import annotations

import json
from typing import TextIO

from .config import ID_PATTERN, ProviderConfigStore, StepPlanConfig
from .keychain import MacOSKeychain


MAX_REQUEST_BYTES = 131_072
STEP_PLAN_FIELDS = frozenset({
    "version", "action", "providerId", "name", "apiKey", "sessionCookie",
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


def run_provider_mutation(
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    store: ProviderConfigStore | None = None,
    keychain: MacOSKeychain | None = None,
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
        if not isinstance(payload, dict) or set(payload) != STEP_PLAN_FIELDS:
            raise ValueError("Provider edit request has unsupported fields")
        if payload.get("version") != 1 or payload.get("action") != "update_step_plan":
            raise ValueError("Provider edit request is unsupported")

        provider_id = _text_field(payload, "providerId", 128, required=True)
        name = _text_field(payload, "name", 160, required=True)
        api_key = _text_field(payload, "apiKey", 65_536)
        session_cookie = _text_field(payload, "sessionCookie", 65_536)
        if ID_PATTERN.fullmatch(provider_id) is None:
            raise ValueError("Provider connection identifier is invalid")

        resolved_store = store or ProviderConfigStore()
        resolved_keychain = keychain or MacOSKeychain()
        configs = resolved_store.load()
        existing = next(
            (
                item for item in configs
                if item.provider_id == provider_id and isinstance(item, StepPlanConfig)
            ),
            None,
        )
        if existing is None:
            return _write_response(
                output_stream, False, "Step Plan connection was not found"
            )

        # Importing the AppKit-facing module is intentionally delayed until the
        # request has passed its strict schema boundary. The controller remains
        # the single owner of Keychain rollback and site-lock validation.
        from .ui import ProviderController

        result = ProviderController(resolved_store, resolved_keychain).update_step_plan(
            StepPlanConfig(provider_id, name, site=existing.site),
            api_key,
            session_cookie,
        )
        return _write_response(output_stream, result.ok, result.message)
    except (json.JSONDecodeError, UnicodeError, ValueError):
        return _write_response(output_stream, False, "Provider edit request is invalid")
    except Exception:
        return _write_response(output_stream, False, "Provider connection could not be updated")
