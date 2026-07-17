import AppKit
import Foundation
import SwiftUI
import Testing
@testable import OpenUsageBar

@Suite("Cross-language Provider visibility")
struct VisibilityStoreTests {
    @Test("Swift default path matches the Python canonical path")
    func canonicalPath() {
        #expect(InstalledPaths.visibility.path.hasSuffix("/.config/openusage-bar/visibility.json"))
    }

    @Test("Python canonical store is readable by Swift")
    func pythonCompatibility() throws {
        let directory = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let visibilityURL = directory.appendingPathComponent("visibility.json")
        let root = repositoryRoot()
        let process = Process()
        process.executableURL = root.appendingPathComponent(".build-venv/bin/python")
        process.arguments = [
            "-c",
            "from pathlib import Path; import sys; from openusage_bar.visibility import ProviderVisibilityStore; ProviderVisibilityStore(Path(sys.argv[1])).save({'claude_code','minimax'})",
            visibilityURL.path,
        ]
        process.currentDirectoryURL = root
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = root.path
        process.environment = environment
        try process.run()
        process.waitUntilExit()
        #expect(process.terminationStatus == 0)

        let snapshot = try VisibilityStore(url: visibilityURL).load()
        #expect(snapshot.hiddenProviderIDs == Set(["claude_code", "minimax"]))
    }

    @Test("Invalid and oversized visibility files are typed and hide nothing")
    func hostileFiles() throws {
        let directory = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("visibility.json")
        try Data(#"{"version":1,"hidden_provider_ids":["valid","../../bad"]}"#.utf8).write(to: url)
        #expect(throws: VisibilityStoreError.invalidSchema) { try VisibilityStore(url: url).load() }
        try Data(#"{"version":1,"hidden_provider_ids":["kiro_cli","kiro_cli"]}"#.utf8).write(to: url)
        #expect(throws: VisibilityStoreError.invalidSchema) { try VisibilityStore(url: url).load() }
        try Data(repeating: 0x20, count: VisibilityStore.maximumBytes + 1).write(to: url)
        #expect(throws: VisibilityStoreError.tooLarge) { try VisibilityStore(url: url).load() }
    }

    @Test("Missing visibility defaults every Provider to visible")
    func missingIsVisible() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let snapshot = try VisibilityStore(url: url).load()
        #expect(snapshot.hiddenProviderIDs.isEmpty)
        #expect(snapshot.revision == 0)
    }

    @Test("Hidden providers disappear from rows and status, then reappear without mutating summary")
    @MainActor
    func liveVisibilityChange() throws {
        let directory = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let ledgerURL = directory.appendingPathComponent("activity.sqlite3")
        try writeLedger(to: ledgerURL)
        let visibilityURL = directory.appendingPathComponent("visibility.json")
        try writeVisibility(["minimax"], to: visibilityURL)
        let model = MenuBarViewModel(ledgerURL: ledgerURL, visibilityURL: visibilityURL)

        model.loadLastGoodOnce()
        #expect(model.summary?.capacity.contains { $0.providerID == "minimax" } == true)
        #expect(!model.groups.flatMap(\.rows).contains { $0.providerID == "minimax" })
        #expect(model.statusLabel.values.isEmpty)

        try writeVisibility([], to: visibilityURL)
        model.checkFreshness()
        #expect(model.groups.flatMap(\.rows).contains { $0.providerID == "minimax" })
        #expect(model.statusLabel.values == ["18%"])

        try writeVisibility(["minimax"], to: visibilityURL)
        model.refresh()
        #expect(!model.groups.flatMap(\.rows).contains { $0.providerID == "minimax" })
        #expect(model.statusLabel.values.isEmpty)
    }

    @Test("Background monitor updates the status item without opening the popover")
    @MainActor
    func backgroundMonitorRefreshesStatus() async throws {
        let directory = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let ledgerURL = directory.appendingPathComponent("activity.sqlite3")
        try writeLedger(to: ledgerURL)
        let visibilityURL = directory.appendingPathComponent("visibility.json")
        try writeVisibility([], to: visibilityURL)
        let model = MenuBarViewModel(ledgerURL: ledgerURL, visibilityURL: visibilityURL)

        model.startMonitoring(interval: 0.02)
        defer { model.stopMonitoring() }
        #expect(model.statusLabel.values == ["18%"])

        try updateQuota(remainingRatio: 0.42, in: ledgerURL)
        for _ in 0..<100 where model.statusLabel.values != ["42%"] {
            try await Task.sleep(for: .milliseconds(10))
        }

        #expect(model.statusLabel.values == ["42%"])
        #expect(model.isMonitoring)
    }

    @Test("Malformed visibility fails open and reports health without exposing contents")
    @MainActor
    func malformedFailsOpen() throws {
        let directory = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let ledgerURL = directory.appendingPathComponent("activity.sqlite3")
        try writeLedger(to: ledgerURL)
        let visibilityURL = directory.appendingPathComponent("visibility.json")
        try Data(#"{"version":1,"hidden_provider_ids":["bad/id","SECRET_TOKEN=value"]}"#.utf8).write(to: visibilityURL)
        let model = MenuBarViewModel(ledgerURL: ledgerURL, visibilityURL: visibilityURL)

        model.loadLastGoodOnce()

        #expect(model.groups.flatMap(\.rows).contains { $0.providerID == "minimax" })
        #expect(model.hasHealthIssues)
        #expect(model.visibilityError == "Provider visibility settings are invalid.")
        #expect(!(model.visibilityError ?? "").contains("SECRET_TOKEN"))
    }

    @Test("Native menu popover renders populated and unavailable states")
    @MainActor
    func nativeMenuRendering() throws {
        let directory = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let ledgerURL = directory.appendingPathComponent("activity.sqlite3")
        let visibilityURL = directory.appendingPathComponent("visibility.json")
        try writeLedger(to: ledgerURL)
        try writeVisibility([], to: visibilityURL)

        let populated = MenuBarViewModel(ledgerURL: ledgerURL, visibilityURL: visibilityURL)
        populated.loadLastGoodOnce()
        #expect(populated.groups.contains(where: { $0.primary.providerID == "minimax" }))
        let expanded = render(MenuBarPopover(model: populated))
        #expect(expanded.size.width == 400)
        #expect(expanded.descendantCount > 8)

        let unavailable = MenuBarViewModel(
            ledgerURL: directory.appendingPathComponent("missing.sqlite3"),
            visibilityURL: visibilityURL
        )
        unavailable.loadLastGoodOnce()
        #expect(unavailable.displayError != nil)
        let empty = render(MenuBarPopover(model: unavailable))
        #expect(empty.size.width == 400)
        #expect(empty.descendantCount > 4)
    }

    private func writeVisibility(_ hidden: [String], to url: URL) throws {
        let payload: [String: Any] = ["version": 1, "hidden_provider_ids": hidden]
        try JSONSerialization.data(withJSONObject: payload).write(to: url, options: .atomic)
    }

    private func writeLedger(to url: URL) throws {
        let root = repositoryRoot()
        let process = Process()
        process.executableURL = root.appendingPathComponent(".build-venv/bin/python")
        process.arguments = [
            "-c",
            """
            import sys
            from openusage_bar.activity_store import ActivityStore, QuotaObservation
            store=ActivityStore(sys.argv[1])
            store.record_quota(QuotaObservation(record_id='minimax.five-hour', observed_at='2026-07-14T08:00:00Z', provider_id='minimax', quota_name='5-hour', unit='percent', used='82', quota_limit='100', remaining='18', remaining_ratio=.18, resets_at='2026-07-14T10:00:00Z', period_start=None, period_end=None, state='ok', quality='live', stale=False))
            store.record_quota(QuotaObservation(record_id='codex.weekly', observed_at='2026-07-14T08:00:00Z', provider_id='codex', quota_name='weekly', unit='tokens', used=None, quota_limit=None, remaining=None, remaining_ratio=None, resets_at=None, period_start=None, period_end=None, state='temporarily_unavailable', quality='cached', stale=True))
            store.close()
            """,
            url.path,
        ]
        process.currentDirectoryURL = root
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = root.path
        process.environment = environment
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw VisibilityFixtureError.python }
    }

    private func updateQuota(remainingRatio: Double, in url: URL) throws {
        let root = repositoryRoot()
        let process = Process()
        process.executableURL = root.appendingPathComponent(".build-venv/bin/python")
        process.arguments = [
            "-c",
            """
            import sys
            from openusage_bar.activity_store import ActivityStore, QuotaObservation
            ratio=float(sys.argv[2])
            store=ActivityStore(sys.argv[1])
            store.record_quota(QuotaObservation(record_id='minimax.five-hour', observed_at='2026-07-14T09:00:00Z', provider_id='minimax', quota_name='5-hour', unit='percent', used=str((1-ratio)*100), quota_limit='100', remaining=str(ratio*100), remaining_ratio=ratio, resets_at='2026-07-14T10:00:00Z', period_start=None, period_end=None, state='ok', quality='live', stale=False))
            store.close()
            """,
            url.path,
            String(remainingRatio),
        ]
        process.currentDirectoryURL = root
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = root.path
        process.environment = environment
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw VisibilityFixtureError.python }
    }

    @MainActor
    private func render<Content: View>(_ content: Content) -> (size: CGSize, descendantCount: Int) {
        let hosting = NSHostingView(rootView: content)
        hosting.frame = NSRect(x: 0, y: 0, width: 400, height: 620)
        let window = NSWindow(
            contentRect: hosting.frame, styleMask: [.borderless],
            backing: .buffered, defer: false
        )
        window.contentView = hosting
        hosting.layoutSubtreeIfNeeded()
        hosting.displayIfNeeded()
        let result = (hosting.frame.size, descendants(of: hosting))
        MenuRenderRetention.windows.append(window)
        return result
    }

    @MainActor
    private func descendants(of view: NSView) -> Int {
        view.subviews.reduce(view.subviews.count) { $0 + descendants(of: $1) }
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    private func repositoryRoot() -> URL {
        URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
    }
}

@MainActor
private enum MenuRenderRetention {
    static var windows: [NSWindow] = []
}

private enum VisibilityFixtureError: Error { case python }
