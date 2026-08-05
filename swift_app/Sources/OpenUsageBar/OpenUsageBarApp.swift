import AppKit
import SwiftUI
import UsageCore

@MainActor
final class AppLaunchDelegate: NSObject, NSApplicationDelegate {
    private var statusController: StatusItemController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        BackgroundServicePresenter.showIfNeeded(BackgroundServiceBootstrap.start())
        let model = MenuBarViewModel()
        let controller = StatusItemController(model: model)
        statusController = controller
        controller.install()
        model.startMonitoring()
        recover(reopened: false)
    }

    func applicationShouldHandleReopen(
        _ sender: NSApplication,
        hasVisibleWindows flag: Bool
    ) -> Bool {
        recover(reopened: true)
        return true
    }

    private func recover(reopened: Bool) {
        Task { @MainActor in
            let decision = RecoveryDecision.decide(
                background: ProcessInfo.processInfo.arguments.contains("--background"),
                reopened: reopened,
                helperAvailable: HelperLauncher.plan(kind: .activity) != nil
            )
            switch decision {
            case .none: break
            case let .launchActivity(route):
                if await HelperLauncher.launchActivity(route: route) != .launched {
                    RecoveryPresenter.show()
                }
            case .showNativeRecovery:
                RecoveryPresenter.show()
            }
        }
    }
}

@main
struct OpenUsageBarApp: App {
    @NSApplicationDelegateAdaptor(AppLaunchDelegate.self) private var appDelegate

    var body: some Scene {
        Settings { EmptyView() }
    }
}

@MainActor
final class StatusItemController: NSObject {
    private let model: MenuBarViewModel
    private var statusItem: NSStatusItem?
    private let popover = NSPopover()

    init(model: MenuBarViewModel) {
        self.model = model
        super.init()
    }

    func install() {
        let item = NSStatusBar.system.statusItem(
            withLength: NSStatusItem.variableLength
        )
        // Use a dedicated autosave name so the item is not restored into the
        // Control Center overflow from an earlier MenuBarExtra lifecycle.
        item.autosaveName = "com.lune.openusagebar.status"
        item.isVisible = true
        if let button = item.button {
            let image = NSImage(
                systemSymbolName: "chart.bar.xaxis",
                accessibilityDescription: model.accessibilityTitle
            )
            button.image = image
            button.imagePosition = .imageLeading
            if image == nil {
                // Never leave the status button content-less; a zero-width
                // invisible item looks like a missing icon.
                button.title = "Hub"
            }
            button.target = self
            button.action = #selector(togglePopover(_:))
            button.toolTip = "UsageHub"
            button.setAccessibilityLabel(model.accessibilityTitle)
        }
        statusItem = item

        popover.behavior = .transient
        popover.contentSize = NSSize(width: 400, height: 540)
        popover.contentViewController = NSHostingController(
            rootView: MenuBarPopover(model: model)
                .tint(DesignTokens.accent)
        )
    }

    @objc private func togglePopover(_ sender: Any?) {
        if popover.isShown {
            popover.performClose(sender)
        } else {
            showPopover(sender)
        }
    }

    private func showPopover(_ sender: Any?) {
        guard let button = statusItem?.button else { return }
        model.loadLastGoodOnce()
        model.checkFreshness()
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
    }
}
