import io
import json
import unittest

from openusage_bar.gateway.accounts import ProviderAccountRef


class GatewayAccountEditorTkTests(unittest.TestCase):
    def test_ui_self_test_uses_tcl_without_requiring_display(self):
        from openusage_bar.gateway.account_editor_tk import run_gateway_account_editor_self_test

        output = io.StringIO()

        code = run_gateway_account_editor_self_test(output)

        self.assertIn(code, (0, 1))
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[0], {"version": 1, "event": "ready"})
        self.assertEqual(events[-1]["version"], 1)
        self.assertEqual(events[-1]["event"], "terminal")
        self.assertIn(events[-1]["state"], {"succeeded", "failed"})
        self.assertIn(events[-1]["code"], {"ok", "service_unavailable"})

    def test_form_specs_are_single_window_trusted_labeled_and_safe_by_default(self):
        from openusage_bar.gateway.account_editor_model import EditorIntent
        from openusage_bar.gateway.account_editor_tk import build_form_spec

        create = build_form_spec(
            EditorIntent(action="gatewayAccount.openCreate", preset="openai")
        )

        self.assertIn("not shown in the dashboard", create.trust_notice)
        self.assertIn("OS credential store", create.trust_notice)
        self.assertEqual([field.key for field in create.fields], ["alias", "provider_key"])
        self.assertEqual(create.fields[0].label, "Account alias")
        self.assertTrue(create.fields[1].secret)
        self.assertGreaterEqual(create.button_min_size, 44)
        self.assertEqual(create.default_button, "cancel")
        self.assertEqual(create.initial_focus_key, "alias")

        account = ProviderAccountRef(
            provider_id="openai",
            account_id="account-0123456789abcdef0123456789abcdef",
            alias="Work",
            credential_account="openai.account-0123456789abcdef0123456789abcdef.gateway-api-key",
        )
        remove = build_form_spec(
            EditorIntent(
                action="gatewayAccount.openRemove",
                display_id=account.display_id,
            ),
            account=account,
        )

        self.assertTrue(remove.requires_confirmation)
        self.assertIn(account.alias, remove.summary)
        self.assertIn(account.display_id, remove.summary)
        self.assertIn(account.provider_id, remove.summary)
        self.assertIn("does not revoke", remove.warning)
        self.assertEqual(remove.primary_label, "Remove")
        self.assertEqual(remove.default_button, "cancel")
        self.assertEqual(remove.initial_focus_key, "cancel")

        replace = build_form_spec(
            EditorIntent(
                action="gatewayAccount.openReplace",
                display_id=account.display_id,
            ),
            account=account,
        )
        self.assertEqual(replace.initial_focus_key, "provider_key")

    def test_form_validation_keeps_window_open_and_uses_generic_error(self):
        from openusage_bar.gateway.account_editor_model import EditorIntent
        from openusage_bar.gateway.account_editor_tk import (
            REQUIRED_FIELD_MESSAGE,
            build_form_spec,
            validate_form_values,
        )

        spec = build_form_spec(
            EditorIntent(action="gatewayAccount.openCreate", preset="openai")
        )

        valid, field, message, values = validate_form_values(
            spec,
            {"alias": "  ", "provider_key": "sk-private"},
        )

        self.assertFalse(valid)
        self.assertEqual(field, "alias")
        self.assertEqual(message, REQUIRED_FIELD_MESSAGE)
        self.assertNotIn("sk-private", message)
        self.assertEqual(values, {})

        valid, field, message, values = validate_form_values(
            spec,
            {"alias": " Work ", "provider_key": ""},
        )

        self.assertFalse(valid)
        self.assertEqual(field, "provider_key")
        self.assertEqual(message, REQUIRED_FIELD_MESSAGE)
        self.assertEqual(values, {})

        valid, field, message, values = validate_form_values(
            spec,
            {"alias": " Work ", "provider_key": "sk-private"},
        )

        self.assertTrue(valid)
        self.assertIsNone(field)
        self.assertEqual(message, "")
        self.assertEqual(values, {"alias": "Work", "provider_key": "sk-private"})

    def test_mapping_success_emits_ready_after_focus_and_mapping_only(self):
        from openusage_bar.gateway.account_editor_tk import map_window_and_emit_ready

        class FakeWidget:
            def __init__(self):
                self.focused = False

            def focus_set(self):
                self.focused = True

        class FakeWindow:
            def __init__(self, *, fail_lift=False):
                self.fail_lift = fail_lift
                self.calls = []

            def update_idletasks(self):
                self.calls.append("update")

            def deiconify(self):
                self.calls.append("deiconify")

            def lift(self):
                self.calls.append("lift")
                if self.fail_lift:
                    raise RuntimeError("map failed")

        ready = []
        widget = FakeWidget()
        window = FakeWindow()

        map_window_and_emit_ready(window, widget, lambda: ready.append("ready"))

        self.assertTrue(widget.focused)
        self.assertEqual(window.calls, ["update", "deiconify", "lift"])
        self.assertEqual(ready, ["ready"])

        ready = []
        with self.assertRaises(RuntimeError):
            map_window_and_emit_ready(
                FakeWindow(fail_lift=True),
                FakeWidget(),
                lambda: ready.append("ready"),
            )
        self.assertEqual(ready, [])

    def test_window_state_binds_activity_idle_warning_timeout_escape_and_cleanup(self):
        from openusage_bar.gateway.account_editor_tk import AccountEditorWindowState

        events = []
        state = AccountEditorWindowState(
            now=lambda: state.clock,
            terminal=lambda status: events.append(status),
            clear=lambda: events.append("clear"),
        )
        state.clock = 0.0

        self.assertEqual(state.bound_events, ())
        state.bind_activity("<KeyPress>")
        state.bind_activity("<ButtonPress>")
        self.assertEqual(state.bound_events, ("<KeyPress>", "<ButtonPress>"))

        state.clock = 530.0
        state.record_activity()
        state.clock = 1069.0
        self.assertIsNone(state.tick())
        state.clock = 1070.0
        self.assertEqual(state.tick(), "idle_warning")
        self.assertTrue(state.warning_visible)
        state.clock = 1130.0
        self.assertEqual(state.tick(), "timed_out")
        self.assertEqual(events, ["timed_out", "clear"])

        events.clear()
        state = AccountEditorWindowState(
            now=lambda: 0.0,
            terminal=lambda status: events.append(status),
            clear=lambda: events.append("clear"),
        )
        self.assertEqual(state.escape(), "cancelled")
        self.assertEqual(events, ["cancelled", "clear"])

        events.clear()
        state = AccountEditorWindowState(
            now=lambda: 0.0,
            terminal=lambda status: events.append(status),
            clear=lambda: events.append("clear"),
        )
        state.begin_saving()
        self.assertEqual(state.escape(), "busy")
        self.assertEqual(state.close(), "busy")
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
