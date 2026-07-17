import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from openusage_bar.config import (
    DailyUsageFeedConfig,
    GenericProviderConfig,
    MiniMaxConfig,
    OpenAIOrganizationConfig,
    StepPlanConfig,
)
from openusage_bar.step_plan import STEP_PLAN_TOKEN_SUFFIX, STEP_PLAN_WEBID_SUFFIX
from openusage_bar.models import Category, Overview, ProviderCard, ProviderStatus
from openusage_bar.ui import (
    FOOTER_HEIGHT,
    HEADER_HEIGHT,
    PANEL_HEIGHT,
    ROW_HEIGHT,
    ProviderController,
    build_sections,
    compact_content_height,
    compact_row_y_positions,
    configure_icon_button,
    configure_manage_button,
    configure_status_item,
    install_standard_edit_menu,
    next_expanded_provider,
    apply_visibility_to_controls,
    update_status_button,
    _build_aggregator,
    create_status_item,
    finish_settings_helper,
    run_provider_settings,
)


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def card(provider_id, category, status=ProviderStatus.OK):
    return ProviderCard(
        provider_id=provider_id,
        name=provider_id,
        category=category,
        status=status,
        primary="value",
        detail=None,
        remaining_percent=50 if category == Category.SUBSCRIPTION else None,
        resets_at=None,
        source="test",
        refreshed_at=NOW,
    )


def generic(endpoint="https://api.example.com/usage"):
    return GenericProviderConfig(
        provider_id="demo",
        name="Demo",
        endpoint=endpoint,
        header_name="Authorization",
        auth_prefix="Bearer",
        primary_path="quota.remaining",
    )


def daily_feed(endpoint="https://api.example.com/daily"):
    return DailyUsageFeedConfig(
        provider_id="glm-work", name="GLM Work", family_id="zai",
        endpoint=endpoint, method="GET", header_name="Authorization",
        auth_prefix="Bearer", items_path="data.items", date_path="day",
        model_path="model", input_tokens_path="input", output_tokens_path="output",
        total_tokens_path="total", since_parameter="from", until_parameter="to",
    )


class UIModelTests(unittest.TestCase):
    def test_openai_setup_requires_admin_key_and_persists_no_secret_in_config(self):
        store = Mock()
        store.load.return_value = []
        keychain = Mock()
        controller = ProviderController(store, keychain)
        config = OpenAIOrganizationConfig("openai", "OpenAI Org")

        missing = controller.add_openai(config, "  ")
        added = controller.add_openai(config, "  admin-secret  ")

        self.assertFalse(missing.ok)
        self.assertTrue(added.ok)
        keychain.set.assert_called_once_with("openai", "admin-secret")
        store.save.assert_called_once_with([config])
        self.assertNotIn("admin-secret", repr(store.save.call_args))

    def test_openai_setup_rejects_duplicate_before_keychain_write(self):
        config = OpenAIOrganizationConfig("openai", "OpenAI Org")
        store = Mock()
        store.load.return_value = [config]
        keychain = Mock()

        result = ProviderController(store, keychain).add_openai(config, "secret")

        self.assertFalse(result.ok)
        keychain.set.assert_not_called()

    def test_update_managed_connection_replaces_minimax_key_and_preserves_type(self):
        existing = MiniMaxConfig("minimax-work", "MiniMax")
        store = Mock()
        store.load.return_value = [existing]
        keychain = Mock()
        keychain.get.return_value = "old-key"

        result = ProviderController(store, keychain).update_connection(
            "minimax-work", "MiniMax Work", "  new-key  "
        )

        self.assertTrue(result.ok)
        keychain.set.assert_called_once_with("minimax-work", "new-key")
        saved = store.save.call_args.args[0]
        self.assertEqual(saved, [MiniMaxConfig("minimax-work", "MiniMax Work")])

    def test_update_managed_connection_keeps_existing_key_when_blank(self):
        existing = generic()
        store = Mock()
        store.load.return_value = [existing]
        keychain = Mock()
        keychain.get.return_value = "saved-key"

        result = ProviderController(store, keychain).update_connection(
            existing.provider_id, "Renamed API", ""
        )

        self.assertTrue(result.ok)
        keychain.set.assert_not_called()
        saved = store.save.call_args.args[0][0]
        self.assertEqual(saved.name, "Renamed API")
        self.assertEqual(saved.endpoint, existing.endpoint)
        self.assertEqual(saved.primary_path, existing.primary_path)

    def test_update_managed_connection_requires_an_existing_or_replacement_key(self):
        existing = daily_feed()
        store = Mock()
        store.load.return_value = [existing]
        keychain = Mock()
        keychain.get.return_value = None

        result = ProviderController(store, keychain).update_connection(
            existing.provider_id, existing.name, ""
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.message, "API key is required")
        store.save.assert_not_called()

    def test_update_managed_connection_rolls_back_replaced_key_on_save_failure(self):
        existing = OpenAIOrganizationConfig("openai", "OpenAI Organization")
        store = Mock()
        store.load.return_value = [existing]
        store.save.side_effect = OSError("disk full")
        keychain = Mock()
        keychain.get.return_value = "old-key"

        result = ProviderController(store, keychain).update_connection(
            "openai", "Work Organization", "new-key"
        )

        self.assertFalse(result.ok)
        self.assertEqual(
            keychain.set.call_args_list,
            [
                unittest.mock.call("openai", "new-key"),
                unittest.mock.call("openai", "old-key"),
            ],
        )

    def test_settings_only_launch_never_creates_a_status_item(self):
        status_bar = Mock()

        self.assertIsNone(create_status_item(status_bar, settings_only=True))

        status_bar.systemStatusBar.assert_not_called()

    def test_settings_close_terminates_only_the_settings_helper(self):
        app = Mock()

        finish_settings_helper(app, settings_only=True)

        app.terminate_.assert_called_once_with(None)

    def test_menu_bar_window_close_does_not_terminate_the_status_host(self):
        app = Mock()

        finish_settings_helper(app, settings_only=False)

        app.terminate_.assert_not_called()

    def test_settings_runner_reuses_the_shared_appkit_implementation(self):
        runner = Mock()

        run_provider_settings(runner=runner)

        runner.assert_called_once_with(settings_only=True)

    def test_builtin_adapter_order_is_openusage_then_kiro_then_codex(self):
        store = Mock()
        store.load.return_value = []
        openusage = Mock(name="openusage")
        kiro = Mock(name="kiro")
        codex = Mock(name="codex")
        client = Mock(name="client")

        with (
            patch("openusage_bar.ui.OpenUsageAdapter", return_value=openusage),
            patch("openusage_bar.ui.KiroQuotaAdapter", return_value=kiro),
            patch("openusage_bar.ui.CodexSubscriptionAdapter", return_value=codex),
            patch("openusage_bar.ui.BoundedHTTPClient", return_value=client),
        ):
            aggregator = _build_aggregator(store, Mock())

        self.assertEqual(aggregator.adapters[:3], [openusage, kiro, codex])

    def test_step_plan_uses_a_site_locked_client_that_cannot_cross_regions(self):
        config = StepPlanConfig(
            "step-plan-main", "Step Plan", site="international"
        )
        store = Mock()
        store.load.return_value = [config]
        default_client = Mock(name="default-client")
        minimax_client = Mock(name="minimax-client")
        step_client = Mock(name="step-client")
        step_adapter = Mock(name="step-adapter")

        with (
            patch(
                "openusage_bar.ui.BoundedHTTPClient",
                side_effect=[default_client, minimax_client, step_client],
            ) as client_factory,
            patch("openusage_bar.ui.OpenUsageAdapter", return_value=Mock()),
            patch("openusage_bar.ui.KiroQuotaAdapter", return_value=Mock()),
            patch("openusage_bar.ui.CodexSubscriptionAdapter", return_value=Mock()),
            patch("openusage_bar.ui.StepPlanAdapter", return_value=step_adapter) as adapter_factory,
        ):
            aggregator = _build_aggregator(store, Mock())

        client_factory.assert_any_call(
            allowed_reserved_hosts={"api.stepfun.ai", "platform.stepfun.ai"},
            allowed_redirect_hosts=set(),
        )
        adapter_factory.assert_called_once_with(config, unittest.mock.ANY, step_client, unittest.mock.ANY)
        self.assertIn(step_adapter, aggregator.adapters)

    def test_visibility_drives_status_title_and_provider_count_together(self):
        status_button = Mock()
        count_label = Mock()
        complete = Overview(
            [
                card("claude_code", Category.LOCAL, ProviderStatus.ERROR),
                card("kiro_cli", Category.SUBSCRIPTION),
            ]
        )

        visible = apply_visibility_to_controls(
            status_button,
            count_label,
            complete,
            {"claude_code"},
        )

        self.assertEqual([item.provider_id for item in visible.cards], ["kiro_cli"])
        status_button.setToolTip_.assert_called_once_with(
            "OpenUsage global overview · OU 50%"
        )
        count_label.setStringValue_.assert_called_once_with("1 Provider")

    def test_manage_button_targets_visibility_management(self):
        button = Mock()
        target = Mock()
        icon = Mock()

        configure_manage_button(button, target, icon)

        button.setTarget_.assert_called_once_with(target)
        button.setAction_.assert_called_once_with("manageProviders:")
        button.setAccessibilityLabel_.assert_called_once_with("Manage providers")
        button.setToolTip_.assert_called_once_with("Manage providers")

    def test_seven_provider_overview_with_attention_fits_default_viewport(self):
        content_height = compact_content_height((3, 1, 3), has_attention=True)

        self.assertLessEqual(
            content_height,
            PANEL_HEIGHT - HEADER_HEIGHT - FOOTER_HEIGHT,
        )

    def test_flipped_row_positions_read_top_to_bottom(self):
        collapsed = compact_row_y_positions(ROW_HEIGHT)
        expanded = compact_row_y_positions(ROW_HEIGHT + 42)

        self.assertLess(collapsed["name"], collapsed["secondary"])
        self.assertLess(collapsed["secondary"], collapsed["quota"])
        self.assertLess(collapsed["quota"], collapsed["separator"])
        self.assertLess(expanded["quota"], expanded["detail"])
        self.assertLess(expanded["detail"], expanded["source"])
        self.assertLess(expanded["source"], expanded["separator"])

    def test_compact_action_is_icon_only_accessible_and_tooltipped(self):
        button = Mock()
        icon = Mock()

        configure_icon_button(button, icon, "Refresh usage")

        button.setTitle_.assert_called_once_with("")
        button.setImage_.assert_called_once_with(icon)
        button.setBordered_.assert_called_once_with(False)
        button.setToolTip_.assert_called_once_with("Refresh usage")
        button.setAccessibilityLabel_.assert_called_once_with("Refresh usage")

    def test_row_registry_allows_only_one_expanded_provider(self):
        self.assertEqual(next_expanded_provider(None, "minimax"), "minimax")
        self.assertIsNone(next_expanded_provider("minimax", "minimax"))
        self.assertEqual(next_expanded_provider("minimax", "codex"), "codex")

    def test_standard_edit_menu_routes_command_v_to_first_responder(self):
        app = Mock()
        main_menu = Mock()
        edit_menu = Mock()
        edit_root = Mock()
        created_items = {}

        def menu_factory(title):
            return main_menu if title == "" else edit_menu

        def item_factory(title, action, key):
            item = edit_root if title == "Edit" else Mock()
            created_items[title] = (item, action, key)
            return item

        install_standard_edit_menu(app, menu_factory, item_factory, lambda: Mock())

        app.setMainMenu_.assert_called_once_with(main_menu)
        edit_root.setSubmenu_.assert_called_once_with(edit_menu)
        self.assertEqual(created_items["Paste"][1:], ("paste:", "v"))
        edit_menu.addItem_.assert_any_call(created_items["Paste"][0])

    def test_status_item_avoids_control_center_managed_visibility(self):
        status_item = Mock()
        button = Mock()
        icon = Mock()
        status_item.button.return_value = button

        configure_status_item(status_item, Mock(), icon)

        status_item.setAutosaveName_.assert_not_called()
        status_item.setVisible_.assert_called_once_with(True)
        button.setImage_.assert_called_once_with(icon)
        icon.setTemplate_.assert_called_once_with(True)
        button.setTransparent_.assert_called_once_with(False)
        button.setAppearsDisabled_.assert_called_once_with(False)
        button.setEnabled_.assert_called_once_with(True)
        button.setTitle_.assert_called_once_with("")
        button.setToolTip_.assert_called_once_with("OpenUsage global overview")

    def test_status_item_stays_icon_only_after_refresh(self):
        button = Mock()
        overview = Overview([card("codex", Category.LOCAL)])

        update_status_button(button, overview)

        button.setTitle_.assert_called_once_with("")
        button.setToolTip_.assert_called_once_with("OpenUsage global overview · OU 1/1")

    def test_groups_each_provider_once_in_its_canonical_category(self):
        auth = card("auth", Category.API, ProviderStatus.AUTH)
        sections = build_sections(
            Overview(
                [
                    auth,
                    card("quota", Category.SUBSCRIPTION),
                    card("api", Category.API),
                    card("local", Category.LOCAL),
                ]
            )
        )

        self.assertEqual(
            [section.title for section in sections],
            ["Subscriptions", "API Providers", "Local Tools"],
        )
        self.assertEqual(
            [provider.provider_id for section in sections for provider in section.cards],
            ["quota", "api", "auth", "local"],
        )
        self.assertEqual(
            sum(provider.provider_id == "auth" for section in sections for provider in section.cards),
            1,
        )

    def test_legacy_kiro_category_still_renders_under_subscriptions(self):
        sections = build_sections(Overview([card("kiro_cli", Category.LOCAL)]))

        self.assertEqual([section.title for section in sections], ["Subscriptions"])
        self.assertEqual(sections[0].cards[0].provider_id, "kiro_cli")

    def test_add_provider_validates_before_keychain_write(self):
        store = Mock()
        store.load.return_value = []
        keychain = Mock()
        controller = ProviderController(store, keychain, resolver=lambda _host: ["93.184.216.34"])

        result = controller.add_generic(generic("http://api.example.com"), "secret")

        self.assertFalse(result.ok)
        keychain.set.assert_not_called()
        store.save.assert_not_called()

    def test_add_daily_feed_validates_then_preserves_existing_configs(self):
        existing = generic()
        store = Mock()
        store.load.return_value = [existing]
        keychain = Mock()
        controller = ProviderController(
            store, keychain, resolver=lambda _host: ["93.184.216.34"]
        )
        configured = daily_feed()

        result = controller.add_daily_feed(configured, " feed-secret ")

        self.assertTrue(result.ok)
        keychain.set.assert_called_once_with("glm-work", "feed-secret")
        store.save.assert_called_once_with([existing, configured])

    def test_add_daily_feed_rejects_http_before_keychain_write(self):
        store = Mock()
        keychain = Mock()
        controller = ProviderController(
            store, keychain, resolver=lambda _host: ["93.184.216.34"]
        )

        result = controller.add_daily_feed(
            daily_feed("http://api.example.com/daily"), "secret"
        )

        self.assertFalse(result.ok)
        keychain.set.assert_not_called()
        store.save.assert_not_called()

    def test_add_step_plan_trims_and_saves_key_in_keychain(self):
        store = Mock()
        store.load.return_value = []
        keychain = Mock()
        controller = ProviderController(store, keychain)
        config = StepPlanConfig("step-plan-main", "Step Plan")

        result = controller.add_step_plan(config, "  plan-secret  ")

        self.assertTrue(result.ok)
        keychain.set.assert_called_once_with("step-plan-main", "plan-secret")
        store.save.assert_called_once_with([config])

    def test_add_step_plan_accepts_full_session_cookie_and_discards_unrelated_cookies(self):
        store = Mock()
        store.load.return_value = []
        keychain = Mock()
        controller = ProviderController(store, keychain)
        config = StepPlanConfig("step-plan-main", "Step Plan")

        result = controller.add_step_plan(
            config,
            "api-key",
            "Oasis-Webid=web-id; __stripe_mid=discard; "
            "Oasis-Token=access...refresh; INGRESSCOOKIE=discard",
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            keychain.set.call_args_list,
            [
                unittest.mock.call("step-plan-main", "api-key"),
                unittest.mock.call(
                    "step-plan-main" + STEP_PLAN_TOKEN_SUFFIX, "access...refresh"
                ),
                unittest.mock.call(
                    "step-plan-main" + STEP_PLAN_WEBID_SUFFIX, "web-id"
                ),
            ],
        )
        self.assertNotIn("stripe", str(keychain.set.call_args_list).lower())

    def test_update_step_plan_replaces_the_selected_accounts_api_key(self):
        existing = StepPlanConfig("step-plan-work", "Work", site="china")
        updated = StepPlanConfig("step-plan-work", "Work Updated", site="china")
        store = Mock()
        store.load.return_value = [existing]
        keychain = Mock()
        keychain.get.return_value = "old-key"

        result = ProviderController(store, keychain).update_step_plan(
            updated, "  new-key  ", ""
        )

        self.assertTrue(result.ok)
        keychain.set.assert_called_once_with("step-plan-work", "new-key")
        store.save.assert_called_once_with([updated])

    def test_update_step_plan_keeps_existing_credentials_when_fields_are_blank(self):
        existing = StepPlanConfig("step-plan-work", "Work", site="china")
        updated = StepPlanConfig("step-plan-work", "Renamed", site="china")
        store = Mock()
        store.load.return_value = [existing]
        keychain = Mock()
        keychain.get.side_effect = lambda account: (
            "saved-key" if account == "step-plan-work" else None
        )

        result = ProviderController(store, keychain).update_step_plan(
            updated, "", ""
        )

        self.assertTrue(result.ok)
        keychain.set.assert_not_called()
        store.save.assert_called_once_with([updated])

    def test_update_step_plan_rejects_blank_fields_without_saved_credentials(self):
        existing = StepPlanConfig("step-plan-work", "Work", site="china")
        store = Mock()
        store.load.return_value = [existing]
        keychain = Mock()
        keychain.get.return_value = None

        result = ProviderController(store, keychain).update_step_plan(
            existing, "", ""
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.message, "Step API key or web session is required")
        keychain.set.assert_not_called()
        store.save.assert_not_called()

    def test_update_step_plan_rejects_site_changes_without_touching_keychain(self):
        existing = StepPlanConfig("step-plan-work", "Work", site="china")
        changed = StepPlanConfig(
            "step-plan-work", "Work", site="international"
        )
        store = Mock()
        store.load.return_value = [existing]
        keychain = Mock()

        result = ProviderController(store, keychain).update_step_plan(
            changed, "new-key", ""
        )

        self.assertFalse(result.ok)
        self.assertIn("site cannot be changed", result.message)
        keychain.set.assert_not_called()
        store.save.assert_not_called()

    def test_update_step_plan_rolls_back_keychain_when_config_save_fails(self):
        existing = StepPlanConfig("step-plan-work", "Work", site="china")
        updated = StepPlanConfig("step-plan-work", "Renamed", site="china")
        store = Mock()
        store.load.return_value = [existing]
        store.save.side_effect = OSError("disk full")
        keychain = Mock()
        keychain.get.return_value = "old-key"

        result = ProviderController(store, keychain).update_step_plan(
            updated, "new-key", ""
        )

        self.assertFalse(result.ok)
        self.assertEqual(
            keychain.set.call_args_list,
            [
                unittest.mock.call("step-plan-work", "new-key"),
                unittest.mock.call("step-plan-work", "old-key"),
            ],
        )

    def test_update_step_plan_rolls_back_partial_session_keychain_write(self):
        existing = StepPlanConfig("step-plan-work", "Work", site="china")
        store = Mock()
        store.load.return_value = [existing]
        keychain = Mock()
        old_values = {
            "step-plan-work" + STEP_PLAN_TOKEN_SUFFIX: "old-token",
            "step-plan-work" + STEP_PLAN_WEBID_SUFFIX: "old-webid",
        }
        keychain.get.side_effect = old_values.get
        keychain.set.side_effect = [None, OSError("write failed"), None, None]

        result = ProviderController(store, keychain).update_step_plan(
            existing,
            "",
            "Oasis-Token=new-token; Oasis-Webid=new-webid",
        )

        self.assertFalse(result.ok)
        self.assertEqual(
            keychain.set.call_args_list,
            [
                unittest.mock.call(
                    "step-plan-work" + STEP_PLAN_TOKEN_SUFFIX, "new-token"
                ),
                unittest.mock.call(
                    "step-plan-work" + STEP_PLAN_WEBID_SUFFIX, "new-webid"
                ),
                unittest.mock.call(
                    "step-plan-work" + STEP_PLAN_TOKEN_SUFFIX, "old-token"
                ),
                unittest.mock.call(
                    "step-plan-work" + STEP_PLAN_WEBID_SUFFIX, "old-webid"
                ),
            ],
        )
        store.save.assert_not_called()

    def test_add_provider_rolls_back_keychain_when_config_save_fails(self):
        store = Mock()
        store.load.return_value = []
        store.save.side_effect = OSError("disk full")
        keychain = Mock()
        controller = ProviderController(store, keychain, resolver=lambda _host: ["93.184.216.34"])

        result = controller.add_generic(generic(), "secret")

        self.assertFalse(result.ok)
        keychain.set.assert_called_once_with("demo", "secret")
        keychain.delete.assert_called_once_with("demo")


if __name__ == "__main__":
    unittest.main()
