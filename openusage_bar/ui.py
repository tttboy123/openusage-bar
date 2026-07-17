from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .aggregator import Aggregator, CardCache
from .config import (
    DailyUsageFeedConfig,
    GenericProviderConfig,
    MiniMaxConfig,
    OpenAIOrganizationConfig,
    ProviderConfigStore,
    StepPlanConfig,
    validate_provider_config,
)
from .daily_feed import DailyUsageFeedCardAdapter
from .codex_subscription import CodexSubscriptionAdapter
from .generic import GenericHTTPSAdapter
from .keychain import MacOSKeychain
from .kiro import KiroQuotaAdapter
from .minimax import MiniMaxCodingPlanAdapter
from .models import Category, Overview, ProviderCard, ProviderStatus, canonical_category
from .network import BoundedHTTPClient, UnsafeEndpoint, resolve_public_addresses, validate_endpoint
from .openusage_adapter import OpenUsageAdapter
from .openai_organization import OpenAIOrganizationCardAdapter
from .presentation import QuotaSeverity, build_attention_summary, humanize_refresh_age, present_row
from .step_plan import (
    STEP_PLAN_TOKEN_SUFFIX,
    STEP_PLAN_WEBID_SUFFIX,
    StepPlanAdapter,
    StepPlanParseError,
    StepPlanSession,
    endpoints_for_site,
)
from .visibility import (
    ProviderVisibilityStore,
    hidden_ids_from_selection,
    visibility_rows,
    visible_overview,
)


HEADER_PATTERN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
PANEL_WIDTH = 440
PANEL_HEIGHT = 620
HEADER_HEIGHT = 70
FOOTER_HEIGHT = 42
SECTION_HEADER_HEIGHT = 24
ROW_HEIGHT = 52
EXPANDED_ROW_HEIGHT = 94
ATTENTION_HEIGHT = 36


@dataclass(frozen=True)
class ProviderSection:
    title: str
    cards: list[ProviderCard]


@dataclass(frozen=True)
class OperationResult:
    ok: bool
    message: str


def configure_status_item(status_item, target, icon):
    """Configure an unmanaged status item that macOS cannot auto-hide by identity."""
    status_item.setVisible_(True)
    button = status_item.button()
    icon.setTemplate_(True)
    button.setImage_(icon)
    button.setTransparent_(False)
    button.setAppearsDisabled_(False)
    button.setEnabled_(True)
    button.setTitle_("")
    button.setToolTip_("OpenUsage global overview")
    button.setTarget_(target)
    button.setAction_("togglePopover:")
    return button


def create_status_item(status_bar, settings_only: bool, item_length=None):
    """Keep the compatibility settings helper independent from the status host."""
    if settings_only:
        return None
    return status_bar.systemStatusBar().statusItemWithLength_(item_length)


def finish_settings_helper(app, settings_only: bool) -> None:
    if settings_only:
        app.terminate_(None)


def configure_icon_button(button, icon, accessibility_label: str):
    button.setTitle_("")
    button.setImage_(icon)
    button.setBordered_(False)
    button.setToolTip_(accessibility_label)
    button.setAccessibilityLabel_(accessibility_label)
    return button


def configure_manage_button(button, target, icon):
    button.setTarget_(target)
    button.setAction_("manageProviders:")
    button.setBordered_(False)
    button.setImage_(icon)
    button.setToolTip_("Manage providers")
    button.setAccessibilityLabel_("Manage providers")
    return button


def next_expanded_provider(current: str | None, selected: str) -> str | None:
    return None if current == selected else selected


def compact_content_height(
    section_counts: tuple[int, ...],
    has_attention: bool,
    expanded_rows: int = 0,
) -> int:
    return (
        20
        + len(section_counts) * SECTION_HEADER_HEIGHT
        + sum(section_counts) * ROW_HEIGHT
        + expanded_rows * (EXPANDED_ROW_HEIGHT - ROW_HEIGHT)
        + (ATTENTION_HEIGHT + 4 if has_attention else 0)
    )


def compact_row_y_positions(row_height: int) -> dict[str, int]:
    positions = {
        "name": 7,
        "secondary": 26,
        "quota": ROW_HEIGHT - 3,
        "separator": row_height - 1,
    }
    if row_height > ROW_HEIGHT:
        positions["detail"] = ROW_HEIGHT + 7
        positions["source"] = ROW_HEIGHT + 24
    return positions


def update_status_button(button, overview: Overview) -> None:
    """Keep the item notch-safe while exposing the aggregate in its tooltip."""
    button.setTitle_("")
    button.setToolTip_(f"OpenUsage global overview · {overview.title}")


def apply_visibility_to_controls(
    status_button,
    provider_count_label,
    overview: Overview,
    hidden_provider_ids: set[str],
) -> Overview:
    visible = visible_overview(overview, hidden_provider_ids)
    update_status_button(status_button, visible)
    count = len(visible.cards)
    provider_count_label.setStringValue_(
        f"{count} Provider{'s' if count != 1 else ''}"
    )
    return visible


EDIT_MENU_COMMANDS = (
    ("Undo", "undo:", "z"),
    ("Redo", "redo:", "Z"),
    None,
    ("Cut", "cut:", "x"),
    ("Copy", "copy:", "c"),
    ("Paste", "paste:", "v"),
    ("Select All", "selectAll:", "a"),
)


def install_standard_edit_menu(app, menu_factory, item_factory, separator_factory) -> None:
    """Install responder-chain editing shortcuts for agent apps without a main menu."""
    main_menu = menu_factory("")
    edit_menu = menu_factory("Edit")
    edit_root = item_factory("Edit", None, "")
    edit_root.setSubmenu_(edit_menu)
    main_menu.addItem_(edit_root)
    for command in EDIT_MENU_COMMANDS:
        item = separator_factory() if command is None else item_factory(*command)
        edit_menu.addItem_(item)
    app.setMainMenu_(main_menu)


def build_sections(overview: Overview) -> list[ProviderSection]:
    groups = [
        (
            "Subscriptions",
            [
                card
                for card in overview.cards
                if canonical_category(card.provider_id, card.category) == Category.SUBSCRIPTION
            ],
        ),
        (
            "API Providers",
            [
                card
                for card in overview.cards
                if canonical_category(card.provider_id, card.category) == Category.API
            ],
        ),
        (
            "Local Tools",
            [
                card
                for card in overview.cards
                if canonical_category(card.provider_id, card.category) == Category.LOCAL
            ],
        ),
    ]
    return [ProviderSection(title, sorted(cards, key=lambda card: card.name.lower())) for title, cards in groups if cards]


def _validate_field_path(path: str | None, required: bool = False) -> None:
    if not path:
        if required:
            raise ValueError("Primary field path is required")
        return
    if any(not segment or not re.fullmatch(r"[A-Za-z0-9_-]+", segment) for segment in path.split(".")):
        raise ValueError("Field paths may contain only names separated by dots")


class ProviderController:
    def __init__(
        self,
        store: ProviderConfigStore,
        keychain: MacOSKeychain,
        resolver: Callable[[str], list[str]] = resolve_public_addresses,
    ) -> None:
        self.store = store
        self.keychain = keychain
        self.resolver = resolver

    def add_minimax(self, config: MiniMaxConfig, secret: str) -> OperationResult:
        if not secret.strip():
            return OperationResult(False, "MiniMax key is required")
        return self._save(config, secret.strip())

    def add_openai(
        self, config: OpenAIOrganizationConfig, secret: str
    ) -> OperationResult:
        if not secret.strip():
            return OperationResult(False, "OpenAI Admin API key is required")
        return self._save(config, secret.strip())

    def add_step_plan(
        self,
        config: StepPlanConfig,
        secret: str,
        session_cookie: str = "",
    ) -> OperationResult:
        return self._configure_step_plan(config, secret, session_cookie, allow_existing=False)

    def update_step_plan(
        self,
        config: StepPlanConfig,
        secret: str,
        session_cookie: str,
    ) -> OperationResult:
        return self._configure_step_plan(config, secret, session_cookie, allow_existing=True)

    def _configure_step_plan(
        self,
        config: StepPlanConfig,
        secret: str,
        session_cookie: str,
        allow_existing: bool,
    ) -> OperationResult:
        credentials: dict[str, str] = {}
        if secret.strip():
            credentials[config.provider_id] = secret.strip()
        try:
            if session_cookie.strip():
                session = StepPlanSession.parse(session_cookie)
                credentials[config.provider_id + STEP_PLAN_TOKEN_SUFFIX] = session.token
                credentials[config.provider_id + STEP_PLAN_WEBID_SUFFIX] = session.webid
            if not credentials:
                raise StepPlanParseError("Step API key or web session is required")

            configs = self.store.load()
            existing = next(
                (item for item in configs if item.provider_id == config.provider_id),
                None,
            )
            if existing is not None and not allow_existing:
                return OperationResult(False, "Provider ID already exists")
            if existing is not None and not isinstance(existing, StepPlanConfig):
                return OperationResult(False, "Provider ID belongs to another provider")

            previous = {account: self.keychain.get(account) for account in credentials}
            for account, value in credentials.items():
                self.keychain.set(account, value)
            try:
                updated = [
                    config if item.provider_id == config.provider_id else item
                    for item in configs
                ]
                if existing is None:
                    updated.append(config)
                self.store.save(updated)
            except Exception:
                for account, old_value in previous.items():
                    if old_value is None:
                        self.keychain.delete(account)
                    else:
                        self.keychain.set(account, old_value)
                raise
            return OperationResult(
                True,
                "Step Plan updated" if existing is not None else "Provider added",
            )
        except StepPlanParseError as error:
            return OperationResult(False, str(error))
        except Exception:
            return OperationResult(False, "Provider could not be saved")

    def add_generic(self, config: GenericProviderConfig, secret: str) -> OperationResult:
        try:
            validate_endpoint(config.endpoint, self.resolver)
            if not HEADER_PATTERN.fullmatch(config.header_name):
                raise ValueError("Header name is invalid")
            if "\r" in config.auth_prefix or "\n" in config.auth_prefix:
                raise ValueError("Authentication prefix is invalid")
            _validate_field_path(config.primary_path, required=True)
            _validate_field_path(config.remaining_percent_path)
            _validate_field_path(config.reset_path)
            _validate_field_path(config.detail_path)
            if not secret.strip():
                raise ValueError("API key is required")
        except (UnsafeEndpoint, ValueError) as error:
            return OperationResult(False, str(error))
        return self._save(config, secret.strip())

    def add_daily_feed(
        self, config: DailyUsageFeedConfig, secret: str
    ) -> OperationResult:
        try:
            validate_endpoint(config.endpoint, self.resolver)
            if not secret.strip():
                raise ValueError("API key is required")
            # ProviderConfigStore owns the complete declarative schema validation.
            # This preflight keeps DNS/SSRF failures ahead of any Keychain write.
            validate_provider_config(config)
        except (UnsafeEndpoint, ValueError, OSError) as error:
            return OperationResult(False, str(error))
        return self._save(config, secret.strip())

    def _save(self, config, secret: str) -> OperationResult:
        try:
            configs = self.store.load()
            if any(existing.provider_id == config.provider_id for existing in configs):
                return OperationResult(False, "Provider ID already exists")
            self.keychain.set(config.provider_id, secret)
            try:
                self.store.save([*configs, config])
            except Exception:
                self.keychain.delete(config.provider_id)
                raise
            return OperationResult(True, "Provider added")
        except Exception:
            return OperationResult(False, "Provider could not be saved")


def _build_aggregator(store: ProviderConfigStore, keychain: MacOSKeychain) -> Aggregator:
    clock = lambda: datetime.now(timezone.utc)
    client = BoundedHTTPClient()
    minimax_client = BoundedHTTPClient(allowed_reserved_hosts={"www.minimaxi.com"})
    adapters = [
        OpenUsageAdapter(clock),
        KiroQuotaAdapter(clock=clock),
        CodexSubscriptionAdapter(clock=clock),
    ]
    try:
        configs = store.load()
    except (OSError, ValueError):
        configs = []
    for config in configs:
        if isinstance(config, MiniMaxConfig):
            adapters.append(MiniMaxCodingPlanAdapter(config, keychain, minimax_client, clock))
        elif isinstance(config, OpenAIOrganizationConfig):
            adapters.append(OpenAIOrganizationCardAdapter(config, keychain, clock))
        elif isinstance(config, StepPlanConfig):
            endpoints = endpoints_for_site(config.site)
            step_plan_client = BoundedHTTPClient(
                allowed_reserved_hosts={
                    endpoints.api_host,
                    endpoints.platform_host,
                },
                allowed_redirect_hosts=set(),
            )
            adapters.append(StepPlanAdapter(config, keychain, step_plan_client, clock))
        elif isinstance(config, DailyUsageFeedConfig):
            adapters.append(DailyUsageFeedCardAdapter(config, keychain, clock))
        elif isinstance(config, GenericProviderConfig):
            adapters.append(GenericHTTPSAdapter(config, keychain, client, clock))
    return Aggregator(adapters, CardCache(), clock)


def _run_appkit(*, settings_only: bool) -> None:  # pragma: no cover - exercised through launchd/AppKit smoke tests.
    from AppKit import (
        NSAlert,
        NSAlertFirstButtonReturn,
        NSAlertSecondButtonReturn,
        NSAlertThirdButtonReturn,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSApplicationActivationPolicyRegular,
        NSBackingStoreBuffered,
        NSBox,
        NSBoxCustom,
        NSBoxSeparator,
        NSButton,
        NSColor,
        NSControlStateValueOff,
        NSControlStateValueOn,
        NSFont,
        NSImage,
        NSImageView,
        NSImageLeft,
        NSLineBreakByTruncatingTail,
        NSPopUpButton,
        NSMakeRect,
        NSMenu,
        NSMenuItem,
        NSMinYEdge,
        NSNoBorder,
        NSPopover,
        NSPopoverBehaviorTransient,
        NSScrollView,
        NSSecureTextField,
        NSSquareStatusItemLength,
        NSStatusBar,
        NSSwitchButton,
        NSTextAlignmentCenter,
        NSTextAlignmentRight,
        NSTextAlignmentLeft,
        NSTextField,
        NSView,
        NSViewController,
        NSWindow,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskMiniaturizable,
        NSWindowStyleMaskTitled,
    )
    from Foundation import NSObject, NSSize, NSTimer
    from PyObjCTools import AppHelper

    def label(text: str, frame, size=12, bold=False, color=None, alignment=NSTextAlignmentLeft):
        field = NSTextField.labelWithString_(text)
        field.setFrame_(frame)
        field.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
        field.setAlignment_(alignment)
        field.setLineBreakMode_(NSLineBreakByTruncatingTail)
        field.setMaximumNumberOfLines_(1)
        field.setToolTip_(text)
        if color is not None:
            field.setTextColor_(color)
        return field

    def symbol(name: str, description: str):
        return NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, description)

    def separator(frame):
        view = NSBox.alloc().initWithFrame_(frame)
        view.setBoxType_(NSBoxSeparator)
        return view

    def fill_bar(frame, color, radius=1):
        view = NSBox.alloc().initWithFrame_(frame)
        view.setTitle_("")
        view.setBoxType_(NSBoxCustom)
        view.setBorderType_(NSNoBorder)
        view.setFillColor_(color)
        view.setCornerRadius_(radius)
        return view

    def input_field(placeholder: str, frame, secure=False):
        field = (NSSecureTextField if secure else NSTextField).alloc().initWithFrame_(frame)
        field.setPlaceholderString_(placeholder)
        return field

    class AppDelegate(NSObject):
        def applicationDidFinishLaunching_(self, _notification):
            self.store = ProviderConfigStore()
            self.visibility_store = ProviderVisibilityStore()
            self.hidden_provider_ids = self.visibility_store.load()
            self.keychain = MacOSKeychain()
            self.provider_controller = ProviderController(self.store, self.keychain)
            self.aggregator = _build_aggregator(self.store, self.keychain)
            self.refreshing = False
            self.collapsed_sections = set()
            self.section_titles = {}
            self.row_ids = {}
            self.row_views = {}
            self.expanded_provider_id = None
            self.attention_provider_id = None
            self.last_updated_at = None
            self.all_overview = Overview([])
            self.last_overview = Overview([])

            self.status_item = create_status_item(
                NSStatusBar, settings_only, NSSquareStatusItemLength
            )
            if settings_only:
                self._build_settings_window()
                threading.Thread(target=self._settings_refresh_worker, daemon=True).start()
                return
            icon = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "chart.bar.xaxis", "OpenUsage global overview"
            )
            configure_status_item(self.status_item, self, icon)
            self.status_item.button().setImagePosition_(NSImageLeft)

            self.popover = NSPopover.alloc().init()
            self.popover.setBehavior_(NSPopoverBehaviorTransient)
            self.popover.setContentSize_(NSSize(PANEL_WIDTH, PANEL_HEIGHT))
            self.controller = NSViewController.alloc().init()
            self.root_view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_WIDTH, PANEL_HEIGHT))
            self.controller.setView_(self.root_view)
            self.popover.setContentViewController_(self.controller)
            self._build_header()
            self._build_scroll_view()
            self._build_footer()
            self.refresh_(None)
            self.refresh_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                300.0, self, "refresh:", None, True
            )
            self.age_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                60.0, self, "updateRefreshAge:", None, True
            )

        def _build_settings_window(self):
            style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
            self.settings_window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, 420, 180), style, NSBackingStoreBuffered, False
            )
            self.settings_window.setTitle_("OpenUsage Bar Settings")
            self.settings_window.setDelegate_(self)
            content = self.settings_window.contentView()
            content.addSubview_(
                label(
                    "Providers and visibility",
                    NSMakeRect(24, 130, 372, 24),
                    16,
                    True,
                )
            )
            content.addSubview_(
                label(
                    "Credentials stay in Keychain. Provider validation is shared with OpenUsage Bar.",
                    NSMakeRect(24, 102, 372, 20),
                    11,
                    False,
                    NSColor.secondaryLabelColor(),
                )
            )
            add = NSButton.buttonWithTitle_target_action_("Add Provider", self, "addProvider:")
            add.setFrame_(NSMakeRect(24, 52, 140, 32))
            content.addSubview_(add)
            self.settings_manage = NSButton.buttonWithTitle_target_action_(
                "Provider Visibility", self, "manageProviders:"
            )
            self.settings_manage.setFrame_(NSMakeRect(176, 52, 180, 32))
            self.settings_manage.setEnabled_(False)
            content.addSubview_(self.settings_manage)
            self.settings_window.center()
            self.settings_window.makeKeyAndOrderFront_(None)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

        def _settings_refresh_worker(self):
            overview = self.aggregator.refresh()
            AppHelper.callAfter(self._apply_settings_overview, overview)

        def _apply_settings_overview(self, overview):
            self.all_overview = overview
            self.last_overview = visible_overview(overview, self.hidden_provider_ids)
            self.settings_manage.setEnabled_(True)

        def windowWillClose_(self, _notification):
            finish_settings_helper(NSApplication.sharedApplication(), settings_only)

        def _build_header(self):
            header_y = PANEL_HEIGHT - HEADER_HEIGHT
            self.root_view.addSubview_(label("OpenUsage", NSMakeRect(18, header_y + 35, 104, 22), 16, True))
            self.provider_count = label(
                "0 Providers",
                NSMakeRect(122, header_y + 37, 150, 18),
                11,
                False,
                NSColor.secondaryLabelColor(),
            )
            self.root_view.addSubview_(self.provider_count)
            self.last_refresh = label(
                "Loading…",
                NSMakeRect(18, header_y + 16, 250, 17),
                10,
                False,
                NSColor.secondaryLabelColor(),
            )
            self.root_view.addSubview_(self.last_refresh)

            self.refresh_button = NSButton.buttonWithTitle_target_action_("", self, "refresh:")
            self.refresh_button.setFrame_(NSMakeRect(356, header_y + 25, 30, 30))
            configure_icon_button(
                self.refresh_button,
                symbol("arrow.clockwise", "Refresh usage"),
                "Refresh usage",
            )
            self.root_view.addSubview_(self.refresh_button)

            add_button = NSButton.buttonWithTitle_target_action_("", self, "addProvider:")
            add_button.setFrame_(NSMakeRect(398, header_y + 25, 30, 30))
            configure_icon_button(add_button, symbol("plus", "Add provider"), "Add provider")
            self.root_view.addSubview_(add_button)
            self.root_view.addSubview_(separator(NSMakeRect(0, header_y, PANEL_WIDTH, 1)))

        def _build_scroll_view(self):
            scroll_height = PANEL_HEIGHT - HEADER_HEIGHT - FOOTER_HEIGHT
            self.scroll = NSScrollView.alloc().initWithFrame_(
                NSMakeRect(0, FOOTER_HEIGHT, PANEL_WIDTH, scroll_height)
            )
            self.scroll.setHasVerticalScroller_(True)
            self.scroll.setAutohidesScrollers_(True)
            self.scroll.setBorderType_(NSNoBorder)
            self.scroll.setDrawsBackground_(False)
            self.document = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_WIDTH, scroll_height))
            self.scroll.setDocumentView_(self.document)
            self.root_view.addSubview_(self.scroll)

        def _build_footer(self):
            self.root_view.addSubview_(separator(NSMakeRect(0, FOOTER_HEIGHT - 1, PANEL_WIDTH, 1)))
            manage = NSButton.buttonWithTitle_target_action_("Manage Providers…", None, None)
            manage.setFrame_(NSMakeRect(12, 6, PANEL_WIDTH - 24, 30))
            configure_manage_button(
                manage,
                self,
                symbol("gearshape", "Manage providers"),
            )
            manage.setImagePosition_(NSImageLeft)
            manage.setFont_(NSFont.systemFontOfSize_(11))
            self.root_view.addSubview_(manage)

        def togglePopover_(self, _sender):
            if self.popover.isShown():
                self.popover.performClose_(None)
            else:
                button = self.status_item.button()
                self.popover.showRelativeToRect_ofView_preferredEdge_(button.bounds(), button, NSMinYEdge)

        def refresh_(self, _sender):
            if self.refreshing:
                return
            self.refreshing = True
            self.last_refresh.setStringValue_("Refreshing…")
            self.refresh_button.setEnabled_(False)
            threading.Thread(target=self._refresh_worker, daemon=True).start()

        def _refresh_worker(self):
            overview = self.aggregator.refresh()
            AppHelper.callAfter(self._apply_overview, overview)

        def _apply_overview(self, overview):
            self.refreshing = False
            self.refresh_button.setEnabled_(True)
            self.all_overview = overview
            self.last_updated_at = datetime.now().astimezone()
            self._apply_visibility()

        def _apply_visibility(self):
            visible = apply_visibility_to_controls(
                self.status_item.button(),
                self.provider_count,
                self.all_overview,
                self.hidden_provider_ids,
            )
            self.last_overview = visible
            self._update_refresh_age()
            self._render_cards(visible)

        def updateRefreshAge_(self, _timer):
            self._update_refresh_age()

        def _update_refresh_age(self):
            if self.refreshing:
                self.last_refresh.setStringValue_("Refreshing…")
                return
            self.last_refresh.setStringValue_(
                humanize_refresh_age(self.last_updated_at, datetime.now().astimezone())
            )

        def _render_cards(self, overview, scroll_to_provider_id=None):
            for subview in list(self.document.subviews()):
                subview.removeFromSuperview()
            sections = build_sections(overview)
            attention = build_attention_summary(overview)
            scroll_height = PANEL_HEIGHT - HEADER_HEIGHT - FOOTER_HEIGHT
            visible_counts = tuple(
                0 if section.title in self.collapsed_sections else len(section.cards)
                for section in sections
            )
            expanded_visible = int(
                self.expanded_provider_id is not None
                and any(
                    section.title not in self.collapsed_sections
                    and any(card.provider_id == self.expanded_provider_id for card in section.cards)
                    for section in sections
                )
            )
            total = compact_content_height(visible_counts, attention is not None, expanded_visible)
            height = max(scroll_height, total)
            self.document.setFrame_(NSMakeRect(0, 0, PANEL_WIDTH, height))
            y = height - 8
            self.section_titles = {}
            self.row_ids = {}
            self.row_views = {}
            self.attention_provider_id = attention.provider_id if attention else None

            if attention:
                y -= ATTENTION_HEIGHT
                self._add_attention_summary(attention, y)
                y -= 4

            for index, section in enumerate(sections):
                collapsed = section.title in self.collapsed_sections
                y -= SECTION_HEADER_HEIGHT
                heading = NSButton.buttonWithTitle_target_action_(section.title.upper(), self, "toggleSection:")
                heading.setFrame_(NSMakeRect(10, y, PANEL_WIDTH - 20, SECTION_HEADER_HEIGHT))
                heading.setBordered_(False)
                heading.setAlignment_(NSTextAlignmentLeft)
                heading.setFont_(NSFont.boldSystemFontOfSize_(10))
                heading.setImage_(
                    symbol("chevron.right" if collapsed else "chevron.down", f"Toggle {section.title}")
                )
                heading.setImagePosition_(NSImageLeft)
                heading.setContentTintColor_(NSColor.secondaryLabelColor())
                heading.setToolTip_(f"{'Expand' if collapsed else 'Collapse'} {section.title}")
                heading.setAccessibilityLabel_(
                    f"{section.title}, {len(section.cards)} providers, {'collapsed' if collapsed else 'expanded'}"
                )
                heading.setTag_(index)
                count = label(
                    str(len(section.cards)),
                    NSMakeRect(PANEL_WIDTH - 68, 5, 30, 17),
                    10,
                    False,
                    NSColor.tertiaryLabelColor(),
                    NSTextAlignmentRight,
                )
                heading.addSubview_(count)
                self.section_titles[index] = section.title
                self.document.addSubview_(heading)
                if collapsed:
                    continue
                for card in section.cards:
                    row_height = EXPANDED_ROW_HEIGHT if card.provider_id == self.expanded_provider_id else ROW_HEIGHT
                    y -= row_height
                    self._add_provider_row(card, y, row_height)

            if not sections:
                self._add_empty_state(height)

            if scroll_to_provider_id:
                row = self.row_views.get(scroll_to_provider_id)
                if row is not None:
                    row.scrollRectToVisible_(row.bounds())

        def _status_color(self, status):
            if status in {ProviderStatus.AUTH, ProviderStatus.ERROR}:
                return NSColor.systemRedColor()
            if status in {ProviderStatus.RATE_LIMITED, ProviderStatus.STALE, ProviderStatus.UNKNOWN}:
                return NSColor.systemOrangeColor()
            return NSColor.secondaryLabelColor()

        def _add_attention_summary(self, summary, y):
            count_text = f" · {summary.issue_count} issues" if summary.issue_count > 1 else ""
            button = NSButton.buttonWithTitle_target_action_(
                f"{summary.message}{count_text}", self, "openAttention:"
            )
            button.setFrame_(NSMakeRect(10, y, PANEL_WIDTH - 20, ATTENTION_HEIGHT))
            button.setBordered_(False)
            button.setAlignment_(NSTextAlignmentLeft)
            button.setFont_(NSFont.systemFontOfSize_(11))
            button.setImage_(symbol(present_row(self._card_for_id(summary.provider_id)).status_icon, summary.message))
            button.setImagePosition_(NSImageLeft)
            button.setContentTintColor_(self._status_color(summary.status))
            button.setToolTip_(summary.message)
            button.setAccessibilityLabel_(f"Attention: {summary.message}{count_text}")
            button.setWantsLayer_(True)
            button.layer().setCornerRadius_(8)
            button.layer().setBackgroundColor_(
                self._status_color(summary.status).colorWithAlphaComponent_(0.10).CGColor()
            )
            self.document.addSubview_(button)

        def _card_for_id(self, provider_id):
            return next(card for card in self.last_overview.cards if card.provider_id == provider_id)

        def openAttention_(self, _sender):
            if self.attention_provider_id is None:
                return
            self.expanded_provider_id = self.attention_provider_id
            self._render_cards(self.last_overview, self.attention_provider_id)

        def toggleSection_(self, sender):
            title = self.section_titles.get(sender.tag())
            if title is None:
                return
            if title in self.collapsed_sections:
                self.collapsed_sections.remove(title)
            else:
                self.collapsed_sections.add(title)
            self._render_cards(self.last_overview)

        def _add_provider_row(self, card, y, row_height):
            display = present_row(card)
            tag = len(self.row_ids)
            row = NSButton.buttonWithTitle_target_action_("", self, "toggleProvider:")
            row.setFrame_(NSMakeRect(10, y, PANEL_WIDTH - 20, row_height))
            row.setBordered_(False)
            row.setTag_(tag)
            row.setToolTip_(f"{display.name} · {display.primary}")
            row.setAccessibilityLabel_(
                f"{display.name}, {display.primary}, {display.secondary}"
            )
            self.row_ids[tag] = card.provider_id
            self.row_views[card.provider_id] = row

            if row_height == EXPANDED_ROW_HEIGHT:
                row.setWantsLayer_(True)
                row.layer().setCornerRadius_(6)
                row.layer().setBackgroundColor_(NSColor.selectedContentBackgroundColor().colorWithAlphaComponent_(0.08).CGColor())

            positions = compact_row_y_positions(row_height)
            name_x = 14
            if display.status_icon:
                icon = NSImageView.imageViewWithImage_(symbol(display.status_icon, display.status_text or "Provider status"))
                icon.setFrame_(NSMakeRect(14, positions["name"] + 2, 14, 14))
                icon.setContentTintColor_(self._status_color(display.status))
                row.addSubview_(icon)
                name_x = 36

            row.addSubview_(label(display.name, NSMakeRect(name_x, positions["name"], 184, 18), 12, True))
            row.addSubview_(
                label(
                    display.primary,
                    NSMakeRect(220, positions["name"], 184, 18),
                    12,
                    True,
                    NSColor.labelColor(),
                    NSTextAlignmentRight,
                )
            )
            row.addSubview_(
                label(
                    display.secondary,
                    NSMakeRect(name_x, positions["secondary"], 272, 16),
                    10,
                    False,
                    NSColor.secondaryLabelColor(),
                )
            )
            if display.reset_label:
                row.addSubview_(
                    label(
                        display.reset_label,
                        NSMakeRect(320, positions["secondary"], 84, 16),
                        10,
                        False,
                        NSColor.secondaryLabelColor(),
                        NSTextAlignmentRight,
                    )
                )

            if display.quota_fraction is not None:
                bar_width = PANEL_WIDTH - 20 - name_x - 14
                row.addSubview_(
                    fill_bar(
                        NSMakeRect(name_x, positions["quota"], bar_width, 2),
                        NSColor.separatorColor().colorWithAlphaComponent_(0.45),
                    )
                )
                quota_color = {
                    QuotaSeverity.NORMAL: NSColor.controlAccentColor(),
                    QuotaSeverity.LOW: NSColor.systemOrangeColor(),
                    QuotaSeverity.CRITICAL: NSColor.systemRedColor(),
                }[display.quota_severity]
                row.addSubview_(
                    fill_bar(
                        NSMakeRect(name_x, positions["quota"], round(bar_width * display.quota_fraction), 2),
                        quota_color,
                    )
                )

            if row_height == EXPANDED_ROW_HEIGHT:
                row.addSubview_(
                    label(
                        display.expanded_detail,
                        NSMakeRect(name_x, positions["detail"], PANEL_WIDTH - 20 - name_x - 14, 16),
                        9,
                        False,
                        NSColor.secondaryLabelColor(),
                    )
                )
                row.addSubview_(
                    label(
                        display.source_label,
                        NSMakeRect(name_x, positions["source"], PANEL_WIDTH - 20 - name_x - 14, 15),
                        9,
                        False,
                        NSColor.tertiaryLabelColor(),
                    )
                )

            row.addSubview_(
                separator(
                    NSMakeRect(name_x, positions["separator"], PANEL_WIDTH - 20 - name_x - 10, 1)
                )
            )
            self.document.addSubview_(row)

        def toggleProvider_(self, sender):
            provider_id = self.row_ids.get(sender.tag())
            if provider_id is None:
                return
            self.expanded_provider_id = next_expanded_provider(
                self.expanded_provider_id, provider_id
            )
            self._render_cards(self.last_overview, provider_id)

        def _add_empty_state(self, height):
            center_y = max(80, height // 2)
            self.document.addSubview_(
                label(
                    "No providers yet",
                    NSMakeRect(70, center_y + 12, PANEL_WIDTH - 140, 22),
                    14,
                    True,
                    None,
                    NSTextAlignmentCenter,
                )
            )
            self.document.addSubview_(
                label(
                    "Add a subscription or HTTPS provider to begin.",
                    NSMakeRect(48, center_y - 10, PANEL_WIDTH - 96, 18),
                    10,
                    False,
                    NSColor.secondaryLabelColor(),
                    NSTextAlignmentCenter,
                )
            )
            add = NSButton.buttonWithTitle_target_action_("Add Provider", self, "addProvider:")
            add.setFrame_(NSMakeRect(150, center_y - 48, 140, 28))
            self.document.addSubview_(add)

        def manageProviders_(self, _sender):
            rows = visibility_rows(self.all_overview, self.hidden_provider_ids)
            alert = NSAlert.alloc().init()
            alert.setMessageText_("Manage Providers")
            alert.setInformativeText_(
                "Choose which providers appear in the menu bar. Hidden providers keep their data and credentials."
            )
            alert.addButtonWithTitle_("Done")
            alert.addButtonWithTitle_("Cancel")

            row_height = 30
            accessory_height = max(42, len(rows) * row_height + 8)
            accessory = NSView.alloc().initWithFrame_(
                NSMakeRect(0, 0, 340, accessory_height)
            )
            checkboxes = {}
            for index, row in enumerate(rows):
                checkbox = NSButton.alloc().initWithFrame_(
                    NSMakeRect(
                        4,
                        accessory_height - (index + 1) * row_height,
                        332,
                        24,
                    )
                )
                checkbox.setButtonType_(NSSwitchButton)
                checkbox.setTitle_(row.name)
                checkbox.setState_(
                    NSControlStateValueOn if row.visible else NSControlStateValueOff
                )
                checkbox.setAccessibilityLabel_(f"Show {row.name} in menu bar")
                accessory.addSubview_(checkbox)
                checkboxes[row.provider_id] = checkbox

            if not rows:
                accessory.addSubview_(
                    label(
                        "No providers discovered yet.",
                        NSMakeRect(4, 10, 332, 20),
                        11,
                        False,
                        NSColor.secondaryLabelColor(),
                    )
                )
            alert.setAccessoryView_(accessory)
            if alert.runModal() != NSAlertFirstButtonReturn:
                return

            checked = {
                provider_id
                for provider_id, checkbox in checkboxes.items()
                if checkbox.state() == NSControlStateValueOn
            }
            hidden = hidden_ids_from_selection(rows, checked)
            try:
                self.visibility_store.save(hidden)
            except (OSError, ValueError):
                self._show_visibility_error()
                return
            self.hidden_provider_ids = hidden
            self.expanded_provider_id = None
            if not settings_only:
                self._apply_visibility()

        def _show_visibility_error(self):
            error = NSAlert.alloc().init()
            error.setMessageText_("Could not save provider visibility")
            error.setInformativeText_(
                "The previous visibility selection is still active."
            )
            error.runModal()

        def addProvider_(self, _sender):
            chooser = NSAlert.alloc().init()
            chooser.setMessageText_("Add Provider")
            chooser.setInformativeText_("Choose a built-in provider or a generic HTTPS API.")
            chooser.addButtonWithTitle_("Step Plan")
            chooser.addButtonWithTitle_("MiniMax")
            chooser.addButtonWithTitle_("OpenAI Organization")
            chooser.addButtonWithTitle_("Daily Token Feed")
            chooser.addButtonWithTitle_("Generic API")
            chooser.addButtonWithTitle_("Cancel")
            response = chooser.runModal()
            if response == NSAlertFirstButtonReturn:
                self._add_step_plan_dialog()
            elif response == NSAlertSecondButtonReturn:
                self._add_minimax_dialog()
            elif response == NSAlertThirdButtonReturn:
                self._add_openai_dialog()
            elif response == NSAlertThirdButtonReturn + 1:
                self._add_daily_feed_dialog()
            elif response == NSAlertThirdButtonReturn + 2:
                self._add_generic_dialog()

        def _add_daily_feed_dialog(self):
            alert = NSAlert.alloc().init()
            alert.setMessageText_("Add Daily Token Feed")
            alert.setInformativeText_(
                "Connect a range-aware HTTPS JSON feed. Field paths use dot notation; credentials stay in macOS Keychain."
            )
            alert.addButtonWithTitle_("Save")
            alert.addButtonWithTitle_("Cancel")
            placeholders = [
                "Display name", "Family ID, e.g. zai", "https://api.example.com/usage",
                "Authorization", "Bearer", "Items path, e.g. data.items",
                "Date path (YYYY-MM-DD)", "Model path", "Input Token path",
                "Output Token path", "Cache read path (optional)",
                "Cache creation path (optional)", "Reasoning Token path (optional)",
                "Total Token path", "Since query parameter", "Until query parameter",
                "API key",
            ]
            row_height = 30
            height = len(placeholders) * row_height + 4
            accessory = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 410, height))
            fields = []
            for index, placeholder in enumerate(placeholders):
                field = input_field(
                    placeholder,
                    NSMakeRect(0, height - (index + 1) * row_height, 410, 24),
                    secure=index == len(placeholders) - 1,
                )
                fields.append(field)
                accessory.addSubview_(field)
            alert.setAccessoryView_(accessory)
            if alert.runModal() != NSAlertFirstButtonReturn:
                return
            values = [field.stringValue().strip() for field in fields]
            (
                name, family_id, endpoint, header, prefix, items, day, model,
                input_tokens, output_tokens, cache_read, cache_creation, reasoning,
                total_tokens, since_parameter, until_parameter, secret,
            ) = values
            provider_id = (
                re.sub(r"[^A-Za-z0-9._-]+", "-", name.lower()).strip("-")
                or f"feed-{int(datetime.now().timestamp())}"
            )
            config = DailyUsageFeedConfig(
                provider_id=provider_id,
                name=name or "Daily Token Feed",
                family_id=family_id or provider_id,
                endpoint=endpoint,
                method="GET",
                header_name=header or "Authorization",
                auth_prefix=prefix,
                items_path=items,
                date_path=day,
                model_path=model,
                input_tokens_path=input_tokens,
                output_tokens_path=output_tokens,
                cache_read_tokens_path=cache_read or None,
                cache_creation_tokens_path=cache_creation or None,
                reasoning_tokens_path=reasoning or None,
                total_tokens_path=total_tokens,
                since_parameter=since_parameter,
                until_parameter=until_parameter,
            )
            self._finish_add(self.provider_controller.add_daily_feed(config, secret))

        def _add_openai_dialog(self):
            alert = NSAlert.alloc().init()
            alert.setMessageText_("Connect OpenAI Organization")
            alert.setInformativeText_(
                "Requires an OpenAI Admin API key. A read-only key is recommended; it stays in macOS Keychain."
            )
            alert.addButtonWithTitle_("Save")
            alert.addButtonWithTitle_("Cancel")
            accessory = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 350, 68))
            name = input_field("Account label", NSMakeRect(0, 38, 350, 24))
            secret = input_field(
                "OpenAI Admin API key", NSMakeRect(0, 6, 350, 24), secure=True
            )
            accessory.addSubview_(name)
            accessory.addSubview_(secret)
            alert.setAccessoryView_(accessory)
            if alert.runModal() != NSAlertFirstButtonReturn:
                return
            config = OpenAIOrganizationConfig(
                "openai", name.stringValue().strip() or "OpenAI Organization"
            )
            result = self.provider_controller.add_openai(
                config, secret.stringValue()
            )
            self._finish_add(result)

        def _add_step_plan_dialog(self):
            alert = NSAlert.alloc().init()
            alert.setMessageText_("Connect StepFun Step Plan")
            alert.setInformativeText_(
                "Choose the matching StepFun site, then paste its Session Cookie. Credentials are never sent across China and International hosts."
            )
            alert.addButtonWithTitle_("Save")
            alert.addButtonWithTitle_("Cancel")
            accessory = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 360, 134))
            site = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(0, 104, 360, 24), False
            )
            site.addItemsWithTitles_(["China (.com)", "International (.ai)"])
            name = input_field("Account label", NSMakeRect(0, 72, 360, 24))
            secret = input_field("Step API key (optional)", NSMakeRect(0, 40, 360, 24), secure=True)
            session_cookie = input_field(
                "Full Session Cookie or Oasis-Token", NSMakeRect(0, 8, 360, 24), secure=True
            )
            accessory.addSubview_(site)
            accessory.addSubview_(name)
            accessory.addSubview_(secret)
            accessory.addSubview_(session_cookie)
            alert.setAccessoryView_(accessory)
            if alert.runModal() != NSAlertFirstButtonReturn:
                return
            selected_site = (
                "international" if site.indexOfSelectedItem() == 1 else "china"
            )
            try:
                existing = next(
                    (
                        item
                        for item in self.store.load()
                        if isinstance(item, StepPlanConfig)
                        and item.site == selected_site
                    ),
                    None,
                )
            except (OSError, ValueError):
                existing = None
            provider_id = (
                existing.provider_id
                if existing is not None
                else f"step-plan-{int(datetime.now().timestamp())}"
            )
            config = StepPlanConfig(
                provider_id,
                name.stringValue().strip()
                or (existing.name if existing is not None else "Step Plan"),
                site=selected_site,
            )
            configure = (
                self.provider_controller.update_step_plan
                if existing is not None
                else self.provider_controller.add_step_plan
            )
            result = configure(config, secret.stringValue(), session_cookie.stringValue())
            self._finish_add(result)

        def _add_minimax_dialog(self):
            alert = NSAlert.alloc().init()
            alert.setMessageText_("Add MiniMax Coding Plan")
            alert.addButtonWithTitle_("Save")
            alert.addButtonWithTitle_("Cancel")
            accessory = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 330, 68))
            name = input_field("Account label", NSMakeRect(0, 38, 330, 24))
            secret = input_field("MiniMax Coding Plan key", NSMakeRect(0, 6, 330, 24), secure=True)
            accessory.addSubview_(name)
            accessory.addSubview_(secret)
            alert.setAccessoryView_(accessory)
            if alert.runModal() != NSAlertFirstButtonReturn:
                return
            provider_id = f"minimax-{int(datetime.now().timestamp())}"
            config = MiniMaxConfig(provider_id, name.stringValue().strip() or "MiniMax")
            result = self.provider_controller.add_minimax(config, secret.stringValue())
            self._finish_add(result)

        def _add_generic_dialog(self):
            alert = NSAlert.alloc().init()
            alert.setMessageText_("Add Generic HTTPS Provider")
            alert.addButtonWithTitle_("Save")
            alert.addButtonWithTitle_("Cancel")
            accessory = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 350, 276))
            fields = [
                input_field("Display name", NSMakeRect(0, 246, 350, 24)),
                input_field("https://api.example.com/usage", NSMakeRect(0, 214, 350, 24)),
                input_field("Authorization", NSMakeRect(0, 182, 350, 24)),
                input_field("Bearer", NSMakeRect(0, 150, 350, 24)),
                input_field("Primary path, e.g. data.remaining", NSMakeRect(0, 118, 350, 24)),
                input_field("Percent path (optional)", NSMakeRect(0, 86, 350, 24)),
                input_field("Reset path (optional)", NSMakeRect(0, 54, 350, 24)),
                input_field("API key", NSMakeRect(0, 22, 350, 24), secure=True),
            ]
            for field in fields:
                accessory.addSubview_(field)
            alert.setAccessoryView_(accessory)
            if alert.runModal() != NSAlertFirstButtonReturn:
                return
            name, endpoint, header, prefix, primary, percent, reset, secret = [field.stringValue().strip() for field in fields]
            provider_id = re.sub(r"[^A-Za-z0-9._-]+", "-", name.lower()).strip("-") or f"api-{int(datetime.now().timestamp())}"
            config = GenericProviderConfig(
                provider_id=provider_id,
                name=name or "API Provider",
                endpoint=endpoint,
                header_name=header or "Authorization",
                auth_prefix=prefix,
                primary_path=primary,
                remaining_percent_path=percent or None,
                reset_path=reset or None,
            )
            result = self.provider_controller.add_generic(config, secret)
            self._finish_add(result)

        def _finish_add(self, result):
            message = NSAlert.alloc().init()
            message.setMessageText_("Provider added" if result.ok else "Could not add provider")
            message.setInformativeText_(result.message)
            message.runModal()
            if result.ok:
                self.aggregator = _build_aggregator(self.store, self.keychain)
                if settings_only:
                    self.settings_manage.setEnabled_(False)
                    threading.Thread(target=self._settings_refresh_worker, daemon=True).start()
                else:
                    self.refresh_(None)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(
        NSApplicationActivationPolicyRegular
        if settings_only
        else NSApplicationActivationPolicyAccessory
    )
    install_standard_edit_menu(
        app,
        lambda title: NSMenu.alloc().initWithTitle_(title),
        lambda title, action, key: NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key),
        NSMenuItem.separatorItem,
    )
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()


def run_menu_bar() -> None:
    _run_appkit(settings_only=False)


def run_provider_settings(runner=None) -> None:
    (runner or _run_appkit)(settings_only=True)
