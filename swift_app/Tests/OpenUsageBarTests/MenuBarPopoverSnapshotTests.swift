import AppKit
import Foundation
import SwiftUI
import Testing
@testable import OpenUsageBar

@Suite("MenuBarPopover snapshots")
struct MenuBarPopoverSnapshotTests {
    @Test("Render populated menu bar popover to PNG")
    @MainActor
    func renderPopulatedPopover() throws {
        let directory = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let ledgerURL = directory.appendingPathComponent("activity.sqlite3")
        let visibilityURL = directory.appendingPathComponent("visibility.json")
        try writeLedger(to: ledgerURL)
        try writeVisibility([], to: visibilityURL)

        let model = MenuBarViewModel(ledgerURL: ledgerURL, visibilityURL: visibilityURL)
        model.loadLastGoodOnce()
        let hosting = NSHostingView(rootView: MenuBarPopover(model: model))
        hosting.frame = NSRect(x: 0, y: 0, width: 400, height: 620)
        let window = NSWindow(
            contentRect: hosting.frame, styleMask: [.borderless],
            backing: .buffered, defer: false
        )
        window.contentView = hosting
        hosting.layoutSubtreeIfNeeded()
        hosting.displayIfNeeded()

        let outputURL = directory.appendingPathComponent("swift-menubar-popover-rendered.png")
        try savePNG(hosting, to: outputURL)
        MenuRenderRetention.windows.append(window)
    }

    @MainActor
    private func savePNG(_ view: NSView, to url: URL) throws {
        guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else {
            throw FixtureError.snapshot
        }
        view.cacheDisplay(in: view.bounds, to: bitmap)
        guard let data = bitmap.representation(using: .png, properties: [:]) else {
            throw FixtureError.snapshot
        }
        try data.write(to: url)
    }

    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
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
        guard process.terminationStatus == 0 else { throw FixtureError.python }
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

private enum FixtureError: Error { case snapshot, python }
