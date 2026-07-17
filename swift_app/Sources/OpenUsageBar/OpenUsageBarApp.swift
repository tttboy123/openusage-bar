import AppKit
import SwiftUI

final class AppLaunchDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        recover(reopened: false)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
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
                if await HelperLauncher.launchActivity(route: route) != .launched { RecoveryPresenter.show() }
            case .showNativeRecovery: RecoveryPresenter.show()
            }
        }
    }
}

@main
struct OpenUsageBarApp: App {
    @NSApplicationDelegateAdaptor(AppLaunchDelegate.self) private var appDelegate
    @State private var model = MenuBarViewModel()

    var body: some Scene {
        MenuBarExtra {
            MenuBarPopover(model: model)
        } label: {
            StatusLabelView(label: model.statusLabel)
                .accessibilityLabel(model.accessibilityTitle)
                .accessibilityValue(model.accessibilityValue)
                .task { model.startMonitoring() }
        }
        .menuBarExtraStyle(.window)
    }
}
