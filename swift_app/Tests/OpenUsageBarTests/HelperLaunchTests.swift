import Foundation
import Testing
@testable import OpenUsageBar
@testable import UsageCore

@Suite("Helper launch and explicit recovery")
struct HelperLaunchTests {
    @Test("Canonical Task 9 application names win over compatibility names")
    func canonicalNames() throws {
        let root = URL(fileURLWithPath: "/Applications/OpenUsage Bar.app/Contents/Helpers")
        let existing = Set([
            root.appendingPathComponent("OpenUsage Activity.app").path,
            root.appendingPathComponent("OpenUsageActivity.app").path,
            root.appendingPathComponent("OpenUsage Provider Settings.app").path,
        ])
        let activity = try #require(HelperLaunchPlan.resolve(kind: .activity, route: "health", helpersURL: root, exists: { existing.contains($0.path) }, isExecutable: { _ in false }))
        let settings = try #require(HelperLaunchPlan.resolve(kind: .settings, route: nil, helpersURL: root, exists: { existing.contains($0.path) }, isExecutable: { _ in false }))
        #expect(activity.target == .application(root.appendingPathComponent("OpenUsage Activity.app")))
        #expect(activity.arguments == ["--route", "health"])
        #expect(settings.target == .application(root.appendingPathComponent("OpenUsage Provider Settings.app")))
        #expect(activity.notificationRoute == .dataHealth)
    }

    @Test("Running application receives an allowlisted route before activation")
    @MainActor
    func applicationRouteSignalOrder() async throws {
        let root = URL(fileURLWithPath: "/Applications/OpenUsage Bar.app/Contents/Helpers")
        let app = root.appendingPathComponent("OpenUsage Activity.app")
        let plan = try #require(HelperLaunchPlan.resolve(
            kind: .activity, route: "capacity", helpersURL: root,
            exists: { $0 == app }, isExecutable: { _ in false }
        ))
        var events: [String] = []
        let service = HelperLaunchService(
            postActivityRoute: { route in events.append("post:\(route.rawValue)") },
            openApplication: { _, _, completion in events.append("open"); completion(nil) },
            runExecutable: { _, _ in false }
        )
        #expect(await service.launch(plan) == .launched)
        #expect(events == ["post:capacity", "open"])
        #expect(ActivityRouteMessage.userInfo(for: .capacity) == ["route": "capacity"])
        #expect(ActivityRouteMessage.decode(["route": "unknown"]) == nil)
    }

    @Test("Unknown routes are not forwarded to argv or native IPC")
    func unknownRouteIgnored() throws {
        let root = URL(fileURLWithPath: "/tmp/helpers")
        let executable = root.appendingPathComponent("OpenUsageActivity")
        let plan = try #require(HelperLaunchPlan.resolve(
            kind: .activity, route: "../../secret", helpersURL: root,
            exists: { _ in false }, isExecutable: { $0 == executable }
        ))
        #expect(plan.arguments.isEmpty)
        #expect(plan.notificationRoute == nil)
    }

    @Test("Executable fallback receives route through direct argv")
    @MainActor
    func executableFallback() async throws {
        let root = URL(fileURLWithPath: "/tmp/helpers")
        let executable = root.appendingPathComponent("OpenUsageActivity")
        let plan = try #require(HelperLaunchPlan.resolve(kind: .activity, route: "capacity", helpersURL: root, exists: { _ in false }, isExecutable: { $0 == executable }))
        var probed: (URL, [String])?
        let service = HelperLaunchService(
            openApplication: { _, _, completion in completion(nil) },
            runExecutable: { url, arguments in probed = (url, arguments); return true }
        )
        #expect(await service.launch(plan) == .launched)
        #expect(probed?.0 == executable)
        #expect(probed?.1 == ["--route", "capacity"])
    }

    @Test("Production executable runner passes route as direct argv")
    @MainActor
    func productionExecutableProbe() async throws {
        let marker = FileManager.default.temporaryDirectory
            .appendingPathComponent("openusage-helper-probe-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: marker) }
        let code = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(' '.join(sys.argv[2:]))"

        #expect(HelperExecutableRunner.run(
            URL(fileURLWithPath: "/usr/bin/python3"),
            ["-c", code, marker.path, "--route", "health"]
        ))

        for _ in 0..<100 where !FileManager.default.fileExists(atPath: marker.path) {
            try await Task.sleep(for: .milliseconds(20))
        }
        #expect(try String(contentsOf: marker, encoding: .utf8) == "--route health")
    }

    @Test("Application completion errors and missing helpers are typed")
    @MainActor
    func typedFailures() async {
        struct ProbeError: Error {}
        let plan = HelperLaunchPlan(target: .application(URL(fileURLWithPath: "/tmp/Activity.app")), arguments: [])
        let service = HelperLaunchService(
            openApplication: { _, _, completion in completion(ProbeError()) },
            runExecutable: { _, _ in false }
        )
        #expect(await service.launch(plan) == .failed)
        #expect(HelperLaunchPlan.resolve(kind: .activity, route: nil, helpersURL: URL(fileURLWithPath: "/missing"), exists: { _ in false }, isExecutable: { _ in false }) == nil)
    }

    @Test("Initial and login launches stay silent while an explicit reopen recovers visibly")
    func recoveryDecision() {
        #expect(RecoveryDecision.decide(background: true, reopened: false, helperAvailable: false) == .none)
        #expect(RecoveryDecision.decide(background: true, reopened: true, helperAvailable: true) == .launchActivity(route: "health"))
        #expect(RecoveryDecision.decide(background: false, reopened: false, helperAvailable: true) == .none)
        #expect(RecoveryDecision.decide(background: false, reopened: true, helperAvailable: true) == .launchActivity(route: "health"))
        #expect(RecoveryDecision.decide(background: false, reopened: false, helperAvailable: false) == .none)
    }
}
