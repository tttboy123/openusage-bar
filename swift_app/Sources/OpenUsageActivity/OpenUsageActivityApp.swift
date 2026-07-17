import AppKit
import SwiftUI
import UsageCore

final class ActivityAppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

@main
struct OpenUsageActivityApp: App {
    @NSApplicationDelegateAdaptor(ActivityAppDelegate.self) private var appDelegate
    @State private var store = ActivityViewStore()
    @State private var coordinator: ActivityRouteCoordinator
    @State private var windowRegistry: ActivityWindowRegistry

    init() {
        NSApplication.shared.setActivationPolicy(.regular)
        let initialRoute = UsageDetailsRoute(arguments: Array(CommandLine.arguments.dropFirst()))
        let windowRegistry = ActivityWindowRegistry()
        let coordinator = ActivityRouteCoordinator(
            initialRoute: initialRoute,
            activate: {
                NSApplication.shared.setActivationPolicy(.regular)
                NSApplication.shared.activate(ignoringOtherApps: true)
            },
            revealExistingWindow: windowRegistry.revealExisting,
            openWindow: {}
        )
        coordinator.startListening()
        _coordinator = State(initialValue: coordinator)
        _windowRegistry = State(initialValue: windowRegistry)
    }

    var body: some Scene {
        WindowGroup("Usage Details", id: "usage-details") {
            ActivityRootView(
                store: store, coordinator: coordinator,
                windowRegistry: windowRegistry
            )
                .frame(minWidth: 900, minHeight: 660)
        }
        .defaultSize(width: 1040, height: 720)
        .commands {
            CommandGroup(after: .appInfo) {
                Button("Refresh Usage") { store.reload() }
                    .keyboardShortcut("r", modifiers: .command)
            }
        }
    }
}
