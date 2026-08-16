"""Trusted Gateway account editor model and command adapter."""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from typing import Callable, Protocol, TextIO, cast

from .accounts import ProviderAccountRef
from .commands import MAX_PROVIDER_KEY_BYTES, run_gateway_account_mutation
from .config import GatewayConfig, GatewayConfigStore


API_VERSION = "gateway-account-host.openusage/v1"
MAX_INTENT_BYTES = 16 * 1024
MAX_ALIAS_BYTES = 256
ALLOWED_PRESETS = frozenset({"openai", "anthropic", "deepseek", "openrouter"})
_DISPLAY_ID_PATTERN = re.compile(r"^acct_[0-9a-f]{12}$")
_CREATE_ACTION = "gatewayAccount.openCreate"
_EDIT_ACTION = "gatewayAccount.openEdit"
_REPLACE_ACTION = "gatewayAccount.openReplace"
_REMOVE_ACTION = "gatewayAccount.openRemove"
_TERMINAL_CODES = frozenset({
    "ok",
    "cancelled",
    "timed_out",
    "invalid_intent",
    "not_found",
    "already_exists",
    "invalid_input",
    "credential_unavailable",
    "config_write_failed",
    "account_in_use",
    "helper_busy",
    "service_unavailable",
})
_MUTATION_CODE_MAP = {
    "ok": "ok",
    "already_exists": "already_exists",
    "not_found": "not_found",
    "invalid_request": "invalid_input",
    "unsupported_provider": "invalid_intent",
    "credential_backend_unavailable": "credential_unavailable",
    "credential_write_failed": "credential_unavailable",
    "credential_delete_failed": "credential_unavailable",
    "credential_rollback_failed": "credential_unavailable",
    "lock_unavailable": "helper_busy",
    "config_write_failed": "config_write_failed",
    "account_id_unavailable": "service_unavailable",
    "pool_references_account": "account_in_use",
}


@dataclass(frozen=True)
class EditorIntent:
    action: str
    preset: str | None = None
    display_id: str | None = None


class EditorView(Protocol):
    def prompt_create(
        self,
        intent: EditorIntent,
        on_ready: Callable[[], None],
    ) -> tuple[str, str] | None: ...
    def prompt_alias(
        self,
        intent: EditorIntent,
        account: ProviderAccountRef,
        on_ready: Callable[[], None],
    ) -> str | None: ...
    def prompt_secret(
        self,
        intent: EditorIntent,
        account: ProviderAccountRef | None = None,
        on_ready: Callable[[], None] = lambda: None,
    ) -> str | None: ...
    def confirm_remove(
        self,
        account: ProviderAccountRef,
        on_ready: Callable[[], None],
    ) -> bool: ...


class EditorStore(Protocol):
    def load(self) -> GatewayConfig: ...


Mutation = Callable[[dict[str, object]], dict[str, object]]


class EditorViewTimedOut(RuntimeError):
    """Raised by a view when the trusted editor idles out."""


class GatewayAccountEditorController:
    def __init__(
        self,
        *,
        store: EditorStore | None = None,
        view: EditorView | Callable[[], EditorView],
        mutation: Mutation | None = None,
    ) -> None:
        self.store = store or GatewayConfigStore()
        self._view_source = view
        self._resolved_view: EditorView | None = None
        self.mutation = mutation or _run_mutation_command

    def _view(self) -> EditorView:
        if self._resolved_view is None:
            source = self._view_source
            if callable(source) and not hasattr(source, "prompt_create"):
                self._resolved_view = cast(EditorView, source())
            else:
                self._resolved_view = cast(EditorView, source)
        return self._resolved_view

    def run_from_stream(self, input_stream: TextIO, output_stream: TextIO) -> int:
        try:
            raw = input_stream.read(MAX_INTENT_BYTES + 1)
            if len(raw.encode("utf-8")) > MAX_INTENT_BYTES:
                raise ValueError("invalid intent")
            payload = json.loads(
                raw,
                object_pairs_hook=_json_object_without_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
            intent = _parse_intent(payload)
        except Exception:
            return _terminal(output_stream, "failed", "invalid_intent")

        if intent.action == _CREATE_ACTION:
            return self._create(intent, output_stream)
        account = self._resolve_account(intent.display_id)
        if account is None:
            return _terminal(output_stream, "failed", "not_found")
        if intent.action == _EDIT_ACTION:
            return self._edit(intent, account, output_stream)
        if intent.action == _REPLACE_ACTION:
            return self._replace(intent, account, output_stream)
        if intent.action == _REMOVE_ACTION:
            return self._remove(account, output_stream)
        return _terminal(output_stream, "failed", "invalid_intent")

    def _create(self, intent: EditorIntent, output_stream: TextIO) -> int:
        if intent.preset not in ALLOWED_PRESETS:
            return _terminal(output_stream, "failed", "invalid_intent")
        try:
            values = self._view().prompt_create(intent, _ready_once(output_stream))
        except EditorViewTimedOut:
            return _terminal(output_stream, "timed_out", "timed_out")
        except Exception:
            return _terminal(output_stream, "failed", "service_unavailable")
        if values is None:
            return _terminal(output_stream, "cancelled", "cancelled")
        alias, secret = values
        if not _valid_alias(alias) or not _valid_secret(secret):
            return _terminal(output_stream, "failed", "invalid_input")
        return self._mutate(
            {
                "version": 1,
                "action": "create_account",
                "account": {"providerId": intent.preset, "alias": alias},
                "credentialMaterial": {"providerKey": secret},
            },
            output_stream,
        )

    def _edit(
        self,
        intent: EditorIntent,
        account: ProviderAccountRef,
        output_stream: TextIO,
    ) -> int:
        try:
            alias = self._view().prompt_alias(
                intent,
                account,
                _ready_once(output_stream),
            )
        except EditorViewTimedOut:
            return _terminal(output_stream, "timed_out", "timed_out")
        except Exception:
            return _terminal(output_stream, "failed", "service_unavailable")
        if alias is None:
            return _terminal(output_stream, "cancelled", "cancelled")
        if not _valid_alias(alias):
            return _terminal(output_stream, "failed", "invalid_input")
        return self._mutate(
            {
                "version": 1,
                "action": "edit_account",
                "account": {"displayId": account.display_id, "alias": alias},
                "credentialMaterial": {"providerKey": None},
            },
            output_stream,
        )

    def _replace(
        self,
        intent: EditorIntent,
        account: ProviderAccountRef,
        output_stream: TextIO,
    ) -> int:
        try:
            secret = self._view().prompt_secret(
                intent,
                account,
                _ready_once(output_stream),
            )
        except EditorViewTimedOut:
            return _terminal(output_stream, "timed_out", "timed_out")
        except Exception:
            return _terminal(output_stream, "failed", "service_unavailable")
        if secret is None:
            return _terminal(output_stream, "cancelled", "cancelled")
        if not _valid_secret(secret):
            return _terminal(output_stream, "failed", "invalid_input")
        return self._mutate(
            {
                "version": 1,
                "action": "edit_account",
                "account": {
                    "displayId": account.display_id,
                    "alias": account.alias,
                },
                "credentialMaterial": {"providerKey": secret},
            },
            output_stream,
        )

    def _remove(
        self,
        account: ProviderAccountRef,
        output_stream: TextIO,
    ) -> int:
        try:
            confirmed = self._view().confirm_remove(
                account,
                _ready_once(output_stream),
            )
        except EditorViewTimedOut:
            return _terminal(output_stream, "timed_out", "timed_out")
        except Exception:
            return _terminal(output_stream, "failed", "service_unavailable")
        if not confirmed:
            return _terminal(output_stream, "cancelled", "cancelled")
        return self._mutate(
            {
                "version": 1,
                "action": "remove_account",
                "account": {"displayId": account.display_id},
                "credentialMaterial": {},
            },
            output_stream,
        )

    def _resolve_account(self, display_id: str | None) -> ProviderAccountRef | None:
        if not _valid_display_id(display_id):
            return None
        try:
            current = self.store.load()
        except Exception:
            return None
        matches = [
            account
            for account in getattr(current, "accounts", ())
            if isinstance(account, ProviderAccountRef)
            and account.display_id == display_id
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    def _mutate(
        self,
        request: dict[str, object],
        output_stream: TextIO,
    ) -> int:
        try:
            response = self.mutation(request)
        except Exception:
            return _terminal(output_stream, "failed", "service_unavailable")
        if not isinstance(response, dict):
            return _terminal(output_stream, "failed", "service_unavailable")
        code = _public_mutation_code(response.get("code"))
        if response.get("ok") is True and code == "ok":
            return _terminal(output_stream, "succeeded", "ok")
        return _terminal(output_stream, "failed", code)


def _run_mutation_command(request: dict[str, object]) -> dict[str, object]:
    input_stream = io.StringIO(json.dumps(request, ensure_ascii=True, separators=(",", ":")))
    output_stream = io.StringIO()
    code = run_gateway_account_mutation(input_stream, output_stream)
    try:
        payload = json.loads(output_stream.getvalue())
    except json.JSONDecodeError:
        return {"version": 1, "ok": False, "code": "service_unavailable"}
    if code != 0 and isinstance(payload, dict) and payload.get("ok") is not True:
        return payload
    if isinstance(payload, dict):
        return payload
    return {"version": 1, "ok": False, "code": "service_unavailable"}


def _parse_intent(value: object) -> EditorIntent:
    if type(value) is not dict:
        raise ValueError("invalid intent")
    action = value.get("action")
    if value.get("apiVersion") != API_VERSION or type(action) is not str:
        raise ValueError("invalid intent")
    if action == _CREATE_ACTION:
        if set(value) != {"apiVersion", "action", "preset"}:
            raise ValueError("invalid intent")
        preset = value.get("preset")
        if type(preset) is not str or preset not in ALLOWED_PRESETS:
            raise ValueError("invalid intent")
        return EditorIntent(action=action, preset=preset)
    if action in {_EDIT_ACTION, _REPLACE_ACTION, _REMOVE_ACTION}:
        if set(value) != {"apiVersion", "action", "displayId"}:
            raise ValueError("invalid intent")
        display_id = value.get("displayId")
        if not _valid_display_id(display_id):
            raise ValueError("invalid intent")
        assert type(display_id) is str
        return EditorIntent(action=action, display_id=display_id)
    raise ValueError("invalid intent")


def _json_object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid intent")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError("invalid intent")


def _valid_display_id(value: object) -> bool:
    return type(value) is str and _DISPLAY_ID_PATTERN.fullmatch(value) is not None


def _valid_alias(value: object) -> bool:
    return type(value) is str and 0 < len(value.encode("utf-8")) <= MAX_ALIAS_BYTES


def _valid_secret(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value.encode("utf-8")) <= MAX_PROVIDER_KEY_BYTES
    )


def _public_mutation_code(value: object) -> str:
    if type(value) is not str:
        return "service_unavailable"
    return _MUTATION_CODE_MAP.get(value, "service_unavailable")


def _write_event(output_stream: TextIO, payload: dict[str, object]) -> None:
    output_stream.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    output_stream.write("\n")
    output_stream.flush()


def _ready_once(output_stream: TextIO) -> Callable[[], None]:
    emitted = False

    def emit() -> None:
        nonlocal emitted
        if not emitted:
            emitted = True
            _write_event(output_stream, {"version": 1, "event": "ready"})

    return emit


def _terminal(output_stream: TextIO, state: str, code: str) -> int:
    if state not in {"succeeded", "cancelled", "timed_out", "failed"}:
        state = "failed"
    if code not in _TERMINAL_CODES:
        code = "service_unavailable"
    _write_event(
        output_stream,
        {"version": 1, "event": "terminal", "state": state, "code": code},
    )
    return 0 if state in {"succeeded", "cancelled"} else 1


__all__ = [
    "API_VERSION",
    "EditorIntent",
    "EditorViewTimedOut",
    "GatewayAccountEditorController",
]
