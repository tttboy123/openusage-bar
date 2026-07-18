import AppKit
import ServiceManagement
import UsageCore

enum BackgroundServiceState: Sendable, Hashable {
    case notRegistered
    case enabled
    case requiresApproval
    case notFound
}

enum BackgroundServiceAction: Sendable, Hashable {
    case registerLoginItem
    case registerCollector
}

struct BackgroundServicePlan: Sendable, Hashable {
    let actions: [BackgroundServiceAction]
    let requiresApproval: Bool
    let hasPackagingError: Bool

    static func make(
        loginItem: BackgroundServiceState,
        collector: BackgroundServiceState,
        legacyLoginItem: Bool = false,
        legacyCollector: Bool = false
    ) -> Self {
        var actions: [BackgroundServiceAction] = []
        if loginItem == .notRegistered && !legacyLoginItem {
            actions.append(.registerLoginItem)
        }
        if collector == .notRegistered && !legacyCollector {
            actions.append(.registerCollector)
        }
        return Self(
            actions: actions,
            requiresApproval: loginItem == .requiresApproval || collector == .requiresApproval,
            hasPackagingError: (loginItem == .notFound && !legacyLoginItem)
                || (collector == .notFound && !legacyCollector)
        )
    }
}

struct BackgroundServiceBootstrapResult: Sendable, Hashable {
    let requiresApproval: Bool
    let hasPackagingError: Bool
    let registrationFailed: Bool

    var needsAttention: Bool {
        requiresApproval || hasPackagingError || registrationFailed
    }
}

@MainActor
enum BackgroundServiceBootstrap {
    static let collectorPlistName = "com.lune.openusagebar.collector.plist"

    static func start() -> BackgroundServiceBootstrapResult {
        let loginItem = SMAppService.mainApp
        let collector = SMAppService.agent(plistName: collectorPlistName)
        let legacy = legacyInstallState()
        let plan = BackgroundServicePlan.make(
            loginItem: state(loginItem.status),
            collector: state(collector.status),
            legacyLoginItem: legacy.loginItem,
            legacyCollector: legacy.collector
        )
        var registrationFailed = false
        for action in plan.actions {
            do {
                switch action {
                case .registerLoginItem: try loginItem.register()
                case .registerCollector: try collector.register()
                }
            } catch {
                registrationFailed = true
            }
        }
        let refreshed = BackgroundServicePlan.make(
            loginItem: state(loginItem.status),
            collector: state(collector.status),
            legacyLoginItem: legacy.loginItem,
            legacyCollector: legacy.collector
        )
        return BackgroundServiceBootstrapResult(
            requiresApproval: plan.requiresApproval || refreshed.requiresApproval,
            hasPackagingError: plan.hasPackagingError || refreshed.hasPackagingError,
            registrationFailed: registrationFailed
        )
    }

    private static func legacyInstallState() -> (loginItem: Bool, collector: Bool) {
        let directory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents", isDirectory: true)
        return (
            FileManager.default.fileExists(
                atPath: directory.appendingPathComponent("com.lune.openusagebar.plist").path
            ),
            FileManager.default.fileExists(
                atPath: directory.appendingPathComponent("com.lune.openusagebar.collector.plist").path
            )
        )
    }

    private static func state(_ status: SMAppService.Status) -> BackgroundServiceState {
        switch status {
        case .notRegistered: .notRegistered
        case .enabled: .enabled
        case .requiresApproval: .requiresApproval
        case .notFound: .notFound
        @unknown default: .notFound
        }
    }
}

@MainActor
enum BackgroundServicePresenter {
    private static var didPresent = false

    static func showIfNeeded(_ result: BackgroundServiceBootstrapResult) {
        guard result.needsAttention, !didPresent else { return }
        didPresent = true
        let alert = NSAlert()
        alert.messageText = AppLocalization.text("Background access needs attention")
        if result.requiresApproval {
            alert.informativeText = AppLocalization.text(
                "Allow OpenUsage Bar in System Settings > General > Login Items so capacity and Token data can refresh automatically."
            )
            alert.addButton(withTitle: AppLocalization.text("Open Login Items"))
            alert.addButton(withTitle: AppLocalization.text("Later"))
            if alert.runModal() == .alertFirstButtonReturn {
                SMAppService.openSystemSettingsLoginItems()
            }
        } else {
            alert.informativeText = AppLocalization.text(
                "OpenUsage Bar could not start its bundled collector. Reinstall the app from the official DMG or use the advanced repair package."
            )
            alert.addButton(withTitle: AppLocalization.text("OK"))
            alert.runModal()
        }
    }
}
