import AppKit
import Foundation
import UsageCore

enum HelperKind: Sendable, Hashable { case activity, settings }

enum HelperTarget: Sendable, Hashable {
    case application(URL)
    case executable(URL)
}

struct HelperLaunchPlan: Sendable, Hashable {
    let target: HelperTarget
    let arguments: [String]
    let notificationRoute: UsageDetailsRoute?

    init(
        target: HelperTarget, arguments: [String],
        notificationRoute: UsageDetailsRoute? = nil
    ) {
        self.target = target
        self.arguments = arguments
        self.notificationRoute = notificationRoute
    }

    static func resolve(
        kind: HelperKind,
        route: String?,
        helpersURL: URL,
        exists: (URL) -> Bool = { FileManager.default.fileExists(atPath: $0.path) },
        isExecutable: (URL) -> Bool = { FileManager.default.isExecutableFile(atPath: $0.path) }
    ) -> Self? {
        let appNames: [String]
        let executableNames: [String]
        switch kind {
        case .activity:
            appNames = ["OpenUsage Activity.app", "OpenUsageActivity.app"]
            executableNames = ["OpenUsageActivity"]
        case .settings:
            appNames = ["OpenUsage Provider Settings.app", "OpenUsageSettings.app"]
            executableNames = ["OpenUsageSettings", "openusage_settings"]
        }
        let notificationRoute = kind == .activity ? route.flatMap(UsageDetailsRoute.init(routeValue:)) : nil
        let arguments = notificationRoute.map { ["--route", $0.transportValue] } ?? []
        if let app = appNames.map(helpersURL.appendingPathComponent).first(where: exists) {
            return Self(
                target: .application(app), arguments: arguments,
                notificationRoute: notificationRoute
            )
        }
        if let executable = executableNames.map(helpersURL.appendingPathComponent).first(where: isExecutable) {
            return Self(target: .executable(executable), arguments: arguments)
        }
        return nil
    }
}

enum HelperLaunchResult: Sendable, Hashable { case launched, unavailable, failed }

@MainActor
enum HelperExecutableRunner {
    private static var activeProcesses: [Process] = []

    static func run(_ url: URL, _ arguments: [String]) -> Bool {
        activeProcesses.removeAll { !$0.isRunning }
        let process = Process()
        process.executableURL = url
        process.arguments = arguments
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            activeProcesses.append(process)
            return true
        } catch {
            return false
        }
    }
}

@MainActor
struct HelperLaunchService {
    typealias ActivityRoutePoster = @MainActor (UsageDetailsRoute) -> Void
    typealias ApplicationOpener = @MainActor (URL, [String], @escaping @MainActor (Error?) -> Void) -> Void
    typealias ExecutableRunner = @MainActor (URL, [String]) -> Bool

    let postActivityRoute: ActivityRoutePoster
    let openApplication: ApplicationOpener
    let runExecutable: ExecutableRunner

    init(
        postActivityRoute: @escaping ActivityRoutePoster = { _ in },
        openApplication: @escaping ApplicationOpener,
        runExecutable: @escaping ExecutableRunner
    ) {
        self.postActivityRoute = postActivityRoute
        self.openApplication = openApplication
        self.runExecutable = runExecutable
    }

    func launch(_ plan: HelperLaunchPlan) async -> HelperLaunchResult {
        switch plan.target {
        case let .application(url):
            if let route = plan.notificationRoute { postActivityRoute(route) }
            return await withCheckedContinuation { continuation in
                openApplication(url, plan.arguments) { error in
                    continuation.resume(returning: error == nil ? .launched : .failed)
                }
            }
        case let .executable(url):
            return runExecutable(url, plan.arguments) ? .launched : .failed
        }
    }

    static let live = Self(
        postActivityRoute: { route in
            DistributedNotificationCenter.default().postNotificationName(
                ActivityRouteMessage.notificationName,
                object: nil,
                userInfo: ActivityRouteMessage.userInfo(for: route),
                deliverImmediately: true
            )
        },
        openApplication: { url, arguments, completion in
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.arguments = arguments
            NSWorkspace.shared.openApplication(at: url, configuration: configuration) { _, error in
                Task { @MainActor in completion(error) }
            }
        },
        runExecutable: HelperExecutableRunner.run
    )
}

enum RecoveryDecision: Sendable, Hashable {
    case none
    case launchActivity(route: String)
    case showNativeRecovery

    static func decide(background: Bool, reopened: Bool, helperAvailable: Bool) -> Self {
        if !reopened { return .none }
        guard helperAvailable else { return .showNativeRecovery }
        return .launchActivity(route: "health")
    }
}

@MainActor
enum HelperLauncher {
    private static var helpersURL: URL { Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers") }

    static func plan(kind: HelperKind, route: String? = nil) -> HelperLaunchPlan? {
        HelperLaunchPlan.resolve(kind: kind, route: route, helpersURL: helpersURL)
    }

    static func launchActivity(route: String? = nil) async -> HelperLaunchResult {
        guard let plan = plan(kind: .activity, route: route) else { return .unavailable }
        return await HelperLaunchService.live.launch(plan)
    }

    static func launchSettings() async -> HelperLaunchResult {
        guard let plan = plan(kind: .settings) else { return .unavailable }
        return await HelperLaunchService.live.launch(plan)
    }

    static func openActivity(route: String? = nil) {
        Task {
            if await launchActivity(route: route) != .launched { RecoveryPresenter.show() }
        }
    }

    static func openHealth() { openActivity(route: "health") }

    static func openSettings() {
        Task {
            if await launchSettings() != .launched { RecoveryPresenter.show() }
        }
    }
}

@MainActor
enum RecoveryPresenter {
    static func show() {
        let alert = NSAlert()
        alert.messageText = AppLocalization.text("OpenUsage Bar needs attention")
        alert.informativeText = AppLocalization.text(
            "The menu-bar item or a helper window is unavailable. Open Data Health, Provider Settings, or macOS Menu Bar settings to repair it."
        )
        alert.addButton(withTitle: AppLocalization.text("Open Data Health"))
        alert.addButton(withTitle: AppLocalization.text("Provider Settings"))
        alert.addButton(withTitle: AppLocalization.text("macOS Menu Bar Settings"))
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            Task { if await HelperLauncher.launchActivity(route: "health") != .launched { openSystemSettings() } }
        case .alertSecondButtonReturn:
            Task { if await HelperLauncher.launchSettings() != .launched { openSystemSettings() } }
        default:
            openSystemSettings()
        }
    }

    private static func openSystemSettings() {
        guard let url = URL(string: "x-apple.systempreferences:com.apple.ControlCenter-Settings.extension") else { return }
        NSWorkspace.shared.open(url)
    }
}
