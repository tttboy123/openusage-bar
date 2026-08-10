"""Tk adapter for the trusted Gateway account editor."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable, TextIO

from .account_editor_model import (
    EditorIntent,
    EditorViewTimedOut,
    GatewayAccountEditorController,
    _write_event,
)
from .accounts import ProviderAccountRef


TRUST_NOTICE = (
    "Provider keys are not shown in the dashboard. They are written only to "
    "the local OS credential store for this device."
)
DELETE_WARNING = (
    "This removes the local Gateway account configuration and stored local "
    "credential. It does not revoke the key at the provider."
)
BUTTON_MIN_SIZE = 44
IDLE_TIMEOUT_SECONDS = 600
IDLE_WARNING_SECONDS = 60
REQUIRED_FIELD_MESSAGE = "Enter the required fields."


@dataclass(frozen=True)
class FormFieldSpec:
    key: str
    label: str
    secret: bool = False
    initial: str = ""


@dataclass(frozen=True)
class AccountEditorFormSpec:
    title: str
    trust_notice: str
    fields: tuple[FormFieldSpec, ...]
    primary_label: str
    default_button: str = "cancel"
    button_min_size: int = BUTTON_MIN_SIZE
    requires_confirmation: bool = False
    summary: str = ""
    warning: str = ""
    provider_id: str | None = None
    initial_focus_key: str = "cancel"


class AccountEditorWindowState:
    def __init__(
        self,
        *,
        now: Callable[[], float],
        terminal: Callable[[str], None],
        clear: Callable[[], None],
        idle_timeout_seconds: int = IDLE_TIMEOUT_SECONDS,
        warning_seconds: int = IDLE_WARNING_SECONDS,
    ) -> None:
        self.now = now
        self.terminal = terminal
        self.clear = clear
        self.idle_timeout_seconds = idle_timeout_seconds
        self.warning_seconds = warning_seconds
        self.last_activity = 0.0
        self.saving = False
        self.closed = False
        self.warning_visible = False
        self._bound_events: list[str] = []

    @property
    def bound_events(self) -> tuple[str, ...]:
        return tuple(self._bound_events)

    def bind_activity(self, event_name: str) -> None:
        self._bound_events.append(event_name)

    def record_activity(self) -> None:
        self.last_activity = self.now()
        self.warning_visible = False

    def begin_saving(self) -> None:
        self.saving = True

    def tick(self) -> str | None:
        if self.closed:
            return None
        elapsed = self.now() - self.last_activity
        if elapsed >= self.idle_timeout_seconds:
            self.closed = True
            self.terminal("timed_out")
            self.clear()
            return "timed_out"
        if elapsed >= self.idle_timeout_seconds - self.warning_seconds:
            self.warning_visible = True
            return "idle_warning"
        return None

    def escape(self) -> str:
        if self.saving:
            return "busy"
        self.closed = True
        self.terminal("cancelled")
        self.clear()
        return "cancelled"

    def close(self) -> str:
        return self.escape()


def build_form_spec(
    intent: EditorIntent,
    *,
    account: ProviderAccountRef | None = None,
) -> AccountEditorFormSpec:
    if intent.action == "gatewayAccount.openCreate":
        provider = intent.preset or ""
        return AccountEditorFormSpec(
            title=f"Add {provider} Gateway account",
            trust_notice=TRUST_NOTICE,
            fields=(
                FormFieldSpec("alias", "Account alias"),
                FormFieldSpec("provider_key", "Provider API key", secret=True),
            ),
            primary_label="Save",
            provider_id=provider,
            initial_focus_key="alias",
        )
    if account is None:
        raise ValueError("account is required")
    if intent.action == "gatewayAccount.openEdit":
        return AccountEditorFormSpec(
            title="Edit Gateway account",
            trust_notice=TRUST_NOTICE,
            fields=(FormFieldSpec("alias", "Account alias", initial=account.alias),),
            primary_label="Save",
            summary=f"{account.alias} ({account.provider_id}, {account.display_id})",
            provider_id=account.provider_id,
            initial_focus_key="alias",
        )
    if intent.action == "gatewayAccount.openReplace":
        return AccountEditorFormSpec(
            title="Replace Gateway credential",
            trust_notice=TRUST_NOTICE,
            fields=(FormFieldSpec("provider_key", "Provider API key", secret=True),),
            primary_label="Save",
            summary=f"{account.alias} ({account.provider_id}, {account.display_id})",
            provider_id=account.provider_id,
            initial_focus_key="provider_key",
        )
    if intent.action == "gatewayAccount.openRemove":
        return AccountEditorFormSpec(
            title="Remove Gateway account",
            trust_notice=TRUST_NOTICE,
            fields=(),
            primary_label="Remove",
            requires_confirmation=True,
            summary=f"{account.alias} ({account.provider_id}, {account.display_id})",
            warning=DELETE_WARNING,
            provider_id=account.provider_id,
        )
    raise ValueError("unsupported editor action")


def validate_form_values(
    spec: AccountEditorFormSpec,
    values: dict[str, str],
) -> tuple[bool, str | None, str, dict[str, str]]:
    normalized: dict[str, str] = {}
    for field in spec.fields:
        value = values.get(field.key, "")
        if field.key == "alias":
            value = value.strip()
        if value == "":
            return False, field.key, REQUIRED_FIELD_MESSAGE, {}
        normalized[field.key] = value
    return True, None, "", normalized


def map_window_and_emit_ready(
    window: object,
    focus_widget: object,
    on_ready: Callable[[], None],
) -> None:
    focus_widget.focus_set()
    window.update_idletasks()
    window.deiconify()
    window.lift()
    on_ready()


class TkGatewayAccountEditorView:
    def __init__(self, root: object | None = None) -> None:
        import tkinter as tk

        self._owns_root = root is None
        self.root = root or tk.Tk()
        try:
            self.root.withdraw()
        except Exception:
            pass

    def prompt_create(
        self,
        intent: EditorIntent,
        on_ready: Callable[[], None],
    ) -> tuple[str, str] | None:
        values = self._run_form(build_form_spec(intent), on_ready)
        if values is None:
            return None
        return values.get("alias", ""), values.get("provider_key", "")

    def prompt_alias(
        self,
        intent: EditorIntent,
        account: ProviderAccountRef,
        on_ready: Callable[[], None],
    ) -> str | None:
        values = self._run_form(build_form_spec(intent, account=account), on_ready)
        if values is None:
            return None
        return values.get("alias", "")

    def prompt_secret(
        self,
        intent: EditorIntent,
        account: ProviderAccountRef | None = None,
        on_ready: Callable[[], None] = lambda: None,
    ) -> str | None:
        values = self._run_form(build_form_spec(intent, account=account), on_ready)
        if values is None:
            return None
        return values.get("provider_key", "")

    def confirm_remove(
        self,
        account: ProviderAccountRef,
        on_ready: Callable[[], None],
    ) -> bool:
        values = self._run_form(
            build_form_spec(
                EditorIntent(
                    action="gatewayAccount.openRemove",
                    display_id=account.display_id,
                ),
                account=account,
            ),
            on_ready,
        )
        return values is not None

    def _run_form(
        self,
        spec: AccountEditorFormSpec,
        on_ready: Callable[[], None],
    ) -> dict[str, str] | None:
        import tkinter as tk
        from tkinter import ttk

        window = tk.Toplevel(self.root)
        window.title(spec.title)
        result: dict[str, str] | None = None
        timed_out = False
        variables: dict[str, object] = {}
        field_widgets: dict[str, object] = {}

        def clear() -> None:
            for variable in variables.values():
                try:
                    variable.set("")
                except Exception:
                    pass

        def terminal(status: str) -> None:
            nonlocal timed_out
            if status == "timed_out":
                timed_out = True
            try:
                window.destroy()
            except Exception:
                pass

        state = AccountEditorWindowState(
            now=time.monotonic,
            terminal=terminal,
            clear=clear,
        )
        state.record_activity()

        frame = ttk.Frame(window, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(frame, text=spec.trust_notice, wraplength=420).grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(0, 12),
        )
        row = 1
        if spec.summary:
            ttk.Label(frame, text=spec.summary).grid(
                row=row,
                column=0,
                columnspan=2,
                sticky="w",
                pady=(0, 8),
            )
            row += 1
        if spec.warning:
            ttk.Label(frame, text=spec.warning, wraplength=420).grid(
                row=row,
                column=0,
                columnspan=2,
                sticky="w",
                pady=(0, 8),
            )
            row += 1
        for field in spec.fields:
            variable = tk.StringVar(value=field.initial)
            variables[field.key] = variable
            ttk.Label(frame, text=field.label).grid(row=row, column=0, sticky="w")
            entry = ttk.Entry(
                frame,
                textvariable=variable,
                show="*" if field.secret else "",
            )
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            field_widgets[field.key] = entry
            row += 1
        confirmed = tk.BooleanVar(value=False)
        variables["_confirmed"] = confirmed
        if spec.requires_confirmation:
            ttk.Checkbutton(
                frame,
                text="I understand this only removes the local credential.",
                variable=confirmed,
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
            row += 1
        warning_text = tk.StringVar(value="")
        variables["_warning"] = warning_text
        ttk.Label(frame, textvariable=warning_text).grid(
            row=row,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(8, 0),
        )
        row += 1
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(12, 0))
        primary_button: object | None = None

        def update_primary_state(*_args: object) -> None:
            if primary_button is not None and spec.requires_confirmation:
                primary_button.configure(
                    state="normal" if confirmed.get() else "disabled"
                )

        def finish_with_values() -> None:
            nonlocal result
            if spec.requires_confirmation and not confirmed.get():
                return
            raw_values = {
                key: variable.get()
                for key, variable in variables.items()
                if key in {field.key for field in spec.fields}
            }
            valid, field_key, message, normalized = validate_form_values(
                spec,
                raw_values,
            )
            if not valid:
                warning_text.set(message)
                if field_key in field_widgets:
                    try:
                        field_widgets[field_key].focus_set()
                    except Exception:
                        pass
                return
            state.begin_saving()
            result = normalized
            clear()
            window.destroy()

        def cancel() -> None:
            nonlocal result
            result = None
            state.escape()

        cancel_button = ttk.Button(buttons, text="Cancel", command=cancel)
        cancel_button.grid(row=0, column=0, padx=(0, 8), ipadx=12, ipady=10)
        primary_button = ttk.Button(
            buttons,
            text=spec.primary_label,
            command=finish_with_values,
        )
        primary_button.grid(row=0, column=1, ipadx=12, ipady=10)
        update_primary_state()
        try:
            confirmed.trace_add("write", update_primary_state)
        except Exception:
            pass

        def activity(_event: object | None = None) -> None:
            state.record_activity()
            warning_text.set("")

        def poll_idle() -> None:
            outcome = state.tick()
            if outcome == "idle_warning":
                warning_text.set("This window will time out in less than 60 seconds.")
            if outcome is None or outcome == "idle_warning":
                try:
                    window.after(1000, poll_idle)
                except Exception:
                    pass

        for event_name in ("<KeyPress>", "<ButtonPress>", "<Motion>"):
            state.bind_activity(event_name)
            window.bind_all(event_name, activity, add="+")
        window.bind("<Escape>", lambda _event: cancel())
        window.protocol("WM_DELETE_WINDOW", cancel)
        window.after(1000, poll_idle)
        focus_widget = (
            field_widgets.get(spec.initial_focus_key)
            if spec.initial_focus_key != "cancel"
            else None
        )
        map_window_and_emit_ready(
            window,
            focus_widget or cancel_button,
            on_ready,
        )
        window.wait_window(window)
        if timed_out:
            raise EditorViewTimedOut()
        return result


def run_gateway_account_editor(
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    view: object | None = None,
) -> int:
    resolved_view = view or TkGatewayAccountEditorView
    controller = GatewayAccountEditorController(view=resolved_view)
    return controller.run_from_stream(input_stream, output_stream)


def run_gateway_account_editor_self_test(output_stream: TextIO) -> int:
    _write_event(output_stream, {"version": 1, "event": "ready"})
    try:
        import tkinter as tk

        interpreter = tk.Tcl()
        patchlevel = interpreter.eval("info patchlevel")
        if not isinstance(patchlevel, str) or not patchlevel:
            raise RuntimeError("Tcl unavailable")
        payload = {
            "version": 1,
            "event": "terminal",
            "state": "succeeded",
            "code": "ok",
        }
        code = 0
    except Exception:
        payload = {
            "version": 1,
            "event": "terminal",
            "state": "failed",
            "code": "service_unavailable",
        }
        code = 1
    output_stream.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    output_stream.write("\n")
    output_stream.flush()
    return code


__all__ = [
    "TkGatewayAccountEditorView",
    "AccountEditorFormSpec",
    "AccountEditorWindowState",
    "FormFieldSpec",
    "REQUIRED_FIELD_MESSAGE",
    "build_form_spec",
    "map_window_and_emit_ready",
    "run_gateway_account_editor",
    "run_gateway_account_editor_self_test",
    "validate_form_values",
]
