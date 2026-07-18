import AppKit
import Foundation
import SQLite3
import SwiftUI
import Testing
@testable import OpenUsageActivity
@testable import UsageCore

@Suite("Usage Details background loading", .serialized)
struct ActivityDataTests {
    @Test("OpenUsage catalog drift is sanitized, visible in Data Health, and non-global")
    func openUsageCatalogPresentation() throws {
        let source = SourceHealthItem(
            providerID: "openusage_catalog", sourceID: "openusage.detect",
            state: "temporarily_unavailable", effectiveState: "temporarily_unavailable",
            lastAttemptAt: "2026-07-15T02:00:00Z", lastSuccessAt: nil,
            staleAt: nil, errorCode: "provider_catalog_drift_e35_a36_m1_x2"
        )
        let status = try #require(OpenUsageCatalogPresentation.from([source]))
        #expect(status.outcome == "provider_catalog_drift")
        #expect(status.expectedCount == GeneratedProviderCatalog.upstreamFamilyIDs.count)
        #expect(status.actualCount == 36)
        #expect(status.missingCount == 1)
        #expect(status.extraCount == 2)
        #expect(status.title == "Provider catalog changed")
        #expect(status.countSummary == "36 detected · 1 missing · 2 extra")
        #expect(!status.isGlobalFailure)
        #expect(OpenUsageCatalogPresentation.isCatalogSource(source))
        #expect(!OpenUsageCatalogPresentation.globalHealthHasIssues([source]))
        let failedProvider = SourceHealthItem(
            providerID: "codex", sourceID: "openusage.daily",
            state: "temporarily_unavailable", effectiveState: "stale",
            lastAttemptAt: "2026-07-15T02:00:00Z", lastSuccessAt: nil,
            staleAt: nil, errorCode: "timeout"
        )
        #expect(OpenUsageCatalogPresentation.globalHealthHasIssues([source, failedProvider]))
    }

    @Test("Malformed OpenUsage diagnostic remains sanitized")
    func malformedOpenUsageCatalogPresentation() throws {
        let source = SourceHealthItem(
            providerID: "openusage_catalog", sourceID: "openusage.detect",
            state: "temporarily_unavailable", effectiveState: "temporarily_unavailable",
            lastAttemptAt: "2026-07-15T02:00:00Z", lastSuccessAt: nil,
            staleAt: nil, errorCode: "credential=/private/example"
        )
        let status = try #require(OpenUsageCatalogPresentation.from([source]))
        #expect(status.outcome == "invalid_detect_output")
        #expect(status.title == "Compatibility check unavailable")
        #expect(!status.title.contains("private"))
    }

    @Test("Actor loads one consistent snapshot and preserves exact daily facts")
    func actorLoad() async throws {
        let fixture = try ActivityLedgerFixture()
        let loader = ActivityDataLoader(databaseURL: fixture.databaseURL, visibilityURL: fixture.visibilityURL)
        let loaded = try await loader.load(request())

        #expect(loaded.details.metrics.totalTokens == 9)
        #expect(loaded.details.chartDays.last?.totalTokens == 9)
        #expect(loaded.visibleProviderIDs == ["codex"])
        #expect(loaded.availableModelIDs == ["gpt-5.5"])
        #expect(loaded.revision == loaded.health.revision)
        #expect(!loaded.visibilityIssue)
        #expect(loaded.details.heatmapDetails.last?.isStale == true)
        #expect(loaded.providerDescriptors["codex"]?.displayName == "Work Codex")
        #expect(loaded.providerDescriptors["codex"]?.category == .subscription)
    }

    @Test("Only stale token-history sources mark heatmap providers stale")
    func relevantTokenFreshness() {
        let sources = [
            SourceHealthItem(
                providerID: "codex", sourceID: "openusage.daily", state: "ok", effectiveState: "ok",
                lastAttemptAt: "2026-07-14T08:00:00Z", lastSuccessAt: "2026-07-14T08:00:00Z",
                staleAt: nil, errorCode: nil
            ),
            SourceHealthItem(
                providerID: "codex", sourceID: "keychain.quota", state: "ok", effectiveState: "stale",
                lastAttemptAt: "2026-07-14T08:00:00Z", lastSuccessAt: nil,
                staleAt: "2026-07-14T08:01:00Z", errorCode: nil
            ),
            SourceHealthItem(
                providerID: "cursor", sourceID: "openusage.daily", state: "ok", effectiveState: "stale",
                lastAttemptAt: "2026-07-14T08:00:00Z", lastSuccessAt: nil,
                staleAt: "2026-07-14T08:01:00Z", errorCode: nil
            ),
        ]
        #expect(ActivityDataLoader.tokenHistoryStaleProviderIDs(sources) == ["cursor"])
    }

    @Test("Header collection time compares parsed instants instead of timestamp text")
    func latestCollectionInstant() {
        #expect(ActivityDataLoader.latestTimestamp([
            "2026-07-14T09:00:00+08:00",
            "2026-07-14T02:00:00Z",
            "not-a-timestamp",
        ]) == "2026-07-14T02:00:00Z")
    }

    @Test("Provider timestamps accept zero through nine fractional digits only")
    func providerTimestampGrammar() {
        for value in [
            "2026-07-14T12:51:06Z",
            "2026-07-14T12:51:06.7Z",
            "2026-07-14T12:51:06.782322Z",
            "2026-07-14T12:51:06.123456789Z",
        ] {
            #expect(ActivityTimestamp.date(from: value) != nil)
            #expect(DateText.display(value) != "Unavailable")
        }
        #expect(ActivityTimestamp.date(from: "2026-07-14T12:51:06.1234567890Z") == nil)
        #expect(ActivityTimestamp.date(from: "not-a-timestamp") == nil)
        #expect(ActivityDataLoader.latestTimestamp([
            "2026-07-14T12:51:06.782322Z",
            "2026-07-14T12:51:07Z",
        ]) == "2026-07-14T12:51:07Z")
    }

    @Test("Day week and month quota bounds use local midnights as UTC half-open instants")
    func localQuotaBounds() throws {
        var calendar = Calendar(identifier: .gregorian)
        let singapore = try #require(TimeZone(identifier: "Asia/Singapore"))
        calendar.timeZone = singapore
        let end = try LocalDay("2026-07-14")
        let expectedStarts: [UsagePeriod: String] = [
            .day: "2026-07-13T16:00:00Z",
            .week: "2026-07-07T16:00:00Z",
            .month: "2026-06-14T16:00:00Z",
        ]
        for (period, expectedStart) in expectedStarts {
            let bounds = try ActivityDataLoader.quotaBounds(
                for: period.range(ending: end), calendar: calendar
            )
            #expect(bounds.start == expectedStart)
            #expect(bounds.endExclusive == "2026-07-14T16:00:00Z")
        }
    }

    @Test("A semantic revision change around quota history retries before publishing")
    func quotaRevisionGate() async throws {
        for phase in [ActivityLoadPhase.beforeQuotaHistory, .afterQuotaHistory] {
            let fixture = try ActivityLedgerFixture()
            let mutation = RevisionMutation(databaseURL: fixture.databaseURL, phase: phase)
            let loader = ActivityDataLoader(
                databaseURL: fixture.databaseURL, visibilityURL: fixture.visibilityURL,
                loadHook: mutation.call
            )
            let loaded = try await loader.load(request())
            let repository = try UsageRepository(databaseURL: fixture.databaseURL)
            let finalRevision = try repository.dataRevision()
            repository.close()

            #expect(loaded.revision == finalRevision)
            #expect(loaded.details.revision == finalRevision)
            #expect(loaded.quotaHistory.count == 1)
            #expect(mutation.calls(for: .beforeQuotaHistory) == 2)
            #expect(mutation.calls(for: .afterQuotaHistory) == 2)
        }
    }

    @Test("Shared visibility hides Claude-style providers without deleting ledger data")
    func visibilityFilter() async throws {
        let fixture = try ActivityLedgerFixture()
        try Data(#"{"version":1,"hidden_provider_ids":["codex"]}"#.utf8).write(to: fixture.visibilityURL)
        let loader = ActivityDataLoader(databaseURL: fixture.databaseURL, visibilityURL: fixture.visibilityURL)
        let loaded = try await loader.load(request())

        #expect(loaded.visibleProviderIDs.isEmpty)
        #expect(loaded.details.heatmapDays.isEmpty)
        #expect(loaded.records.isEmpty)
        #expect(loaded.hiddenProviderIDs == ["codex"])
        #expect(loaded.providerDescriptors["codex"]?.displayName == "Work Codex")
        #expect(loaded.providerInstances.isEmpty)
    }

    @Test("Invalid or hidden selected provider is explicit no-match and leaks no other provider facts")
    func selectedProviderNoMatch() async throws {
        let fixture = try ActivityLedgerFixture()
        let loader = ActivityDataLoader(databaseURL: fixture.databaseURL, visibilityURL: fixture.visibilityURL)
        let invalid = try await loader.load(request(providerID: "not-present"))
        #expect(invalid.selectionMatch == .noMatchingProvider("not-present"))
        #expect(invalid.records.isEmpty)
        #expect(invalid.details.heatmapDays.isEmpty)
        #expect(invalid.capacity.isEmpty)
        #expect(invalid.quotaHistory.isEmpty)

        try Data(#"{"version":1,"hidden_provider_ids":["codex"]}"#.utf8).write(to: fixture.visibilityURL)
        let hidden = try await loader.load(request(providerID: "codex"))
        #expect(hidden.selectionMatch == .noMatchingProvider("codex"))
        #expect(hidden.records.isEmpty)
        #expect(hidden.capacity.isEmpty)
        #expect(hidden.quotaHistory.isEmpty)
    }

    @MainActor
    @Test("Returning from settings hides a newly hidden selected provider before revalidation finishes")
    func dynamicVisibilityRevalidation() async throws {
        let fixture = try ActivityLedgerFixture()
        let preferences = ActivityPreferences(defaults: InMemoryActivityPreferencesStore())
        let store = ActivityViewStore(
            loader: ActivityDataLoader(databaseURL: fixture.databaseURL, visibilityURL: fixture.visibilityURL),
            preferences: preferences
        )
        store.updateFilters(period: .day, providerID: "codex", modelID: nil)
        try await wait(for: store)
        #expect(store.displayData?.selectionMatch == .matched)

        try Data(#"{"version":1,"hidden_provider_ids":["codex"]}"#.utf8).write(to: fixture.visibilityURL)
        store.revalidateSelection()
        #expect(store.displayData == nil)
        try await wait(for: store)
        #expect(store.displayData?.selectionMatch == .noMatchingProvider("codex"))
        #expect(store.displayData?.capacity.isEmpty == true)
    }

    @Test("Invalid model is no-match instead of covered zero and model filter preserves provider quota scope")
    func selectedModelNoMatch() async throws {
        let fixture = try ActivityLedgerFixture()
        let loader = ActivityDataLoader(databaseURL: fixture.databaseURL, visibilityURL: fixture.visibilityURL)
        let loaded = try await loader.load(request(modelID: "not-present"))
        let baseline = try await loader.load(request())
        #expect(loaded.selectionMatch == .noMatchingModel("not-present"))
        #expect(loaded.details.heatmapDays.isEmpty)
        #expect(loaded.records.isEmpty)
        #expect(loaded.capacity == baseline.capacity)
        #expect(loaded.quotaHistory == baseline.quotaHistory)
    }

    @Test("Malformed visibility fails open and becomes a health issue")
    func malformedVisibility() async throws {
        let fixture = try ActivityLedgerFixture()
        try Data(#"{"version":1,"hidden_provider_ids":["../private"]}"#.utf8).write(to: fixture.visibilityURL)
        let loader = ActivityDataLoader(databaseURL: fixture.databaseURL, visibilityURL: fixture.visibilityURL)
        let loaded = try await loader.load(request())

        #expect(loaded.visibleProviderIDs == ["codex"])
        #expect(loaded.visibilityIssue)
        #expect(loaded.health.hasIssues)
    }

    @Test("Missing database returns only the typed sanitized failure")
    func missingDatabase() async {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let loader = ActivityDataLoader(
            databaseURL: directory.appendingPathComponent("private.sqlite"),
            visibilityURL: directory.appendingPathComponent("visibility.json")
        )
        await #expect(throws: RepositoryError.databaseUnavailable) {
            try await loader.load(request())
        }
    }

    @MainActor
    @Test("Main actor store publishes the actor result and clears loading")
    func storePublication() async throws {
        let fixture = try ActivityLedgerFixture()
        let preferences = ActivityPreferences(defaults: InMemoryActivityPreferencesStore())
        let store = ActivityViewStore(loader: ActivityDataLoader(
            databaseURL: fixture.databaseURL, visibilityURL: fixture.visibilityURL
        ), preferences: preferences)
        store.reload()
        for _ in 0..<100 where store.isLoading {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(!store.isLoading)
        #expect(store.error == nil)
        #expect(store.data?.records.map(\.totalTokens) == [9])
        store.cancel()
    }

    @MainActor
    @Test("Filter change clears both focus surfaces synchronously and persists through failure or cancel")
    func synchronousFocusInvalidation() async {
        let loader = FailingActivityLoader()
        let preferences = ActivityPreferences(defaults: InMemoryActivityPreferencesStore())
        let store = ActivityViewStore(loader: loader, preferences: preferences)
        store.chartFocus.day = try! LocalDay("2026-07-02")
        store.heatmapFocusDay = try! LocalDay("2026-07-02")
        store.updateFilters(period: .week, providerID: "codex", modelID: "gpt-5.5")
        #expect(store.chartFocus.day == nil)
        #expect(store.heatmapFocusDay == nil)
        await Task.yield()
        #expect(store.chartFocus.day == nil)
        #expect(store.heatmapFocusDay == nil)
        store.cancel()
        #expect(store.chartFocus.day == nil)
        #expect(store.heatmapFocusDay == nil)
    }

    @MainActor
    @Test("Consecutive routes reuse an existing window and unknown input does nothing")
    func existingInstanceRoute() {
        var activations = 0
        var reveals = 0
        var opens = 0
        let coordinator = ActivityRouteCoordinator(
            initialRoute: .activity,
            activate: { activations += 1 },
            revealExistingWindow: { reveals += 1; return true },
            openWindow: { opens += 1 }
        )
        coordinator.receive(userInfo: ["route": "health"])
        #expect(coordinator.route == .dataHealth)
        #expect(activations == 1)
        #expect(reveals == 1)
        #expect(opens == 0)
        coordinator.receive(userInfo: ["route": "capacity"])
        #expect(coordinator.route == .capacity)
        #expect(activations == 2)
        #expect(reveals == 2)
        #expect(opens == 0)
        coordinator.receive(userInfo: ["route": "../../secret"])
        #expect(coordinator.route == .capacity)
        #expect(activations == 2)
        #expect(reveals == 2)
        #expect(opens == 0)
    }

    @MainActor
    @Test("A minimized registered window is restored without increasing window count")
    func minimizedWindowReuse() {
        let registry = ActivityWindowRegistry()
        let window = MiniaturizedTestWindow()
        registry.register(window)
        #expect(window.isMiniaturized)
        let registeredBefore = registry.registeredWindowCount

        var opens = 0
        let coordinator = ActivityRouteCoordinator(
            initialRoute: .activity, activate: {},
            revealExistingWindow: registry.revealExisting,
            openWindow: { opens += 1 }
        )
        coordinator.receive(userInfo: ["route": "health"])
        #expect(coordinator.route == .dataHealth)
        #expect(!window.isMiniaturized)
        #expect(window.didMakeKeyAndOrderFront)
        #expect(registry.registeredWindowCount == registeredBefore)
        #expect(opens == 0)
        registry.unregister(window)
        RenderRetention.windows.append(window)
    }

    @MainActor
    @Test("Closing the registered window makes the next valid route open exactly one window")
    func closedWindowReopen() {
        let registry = ActivityWindowRegistry()
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 900, height: 660),
            styleMask: [.titled, .closable], backing: .buffered, defer: false
        )
        registry.register(window)
        #expect(registry.registeredWindowCount == 1)
        NotificationCenter.default.post(name: NSWindow.willCloseNotification, object: window)
        #expect(registry.registeredWindowCount == 0)

        var opens = 0
        let coordinator = ActivityRouteCoordinator(
            initialRoute: .activity, activate: {},
            revealExistingWindow: registry.revealExisting,
            openWindow: { opens += 1 }
        )
        coordinator.receive(userInfo: ["route": "capacity"])
        #expect(coordinator.route == .capacity)
        #expect(opens == 1)
        RenderRetention.windows.append(window)
    }

    @MainActor
    @Test("Distributed route signal switches an existing coordinator")
    func distributedExistingRoute() async throws {
        var opens = 0
        let coordinator = ActivityRouteCoordinator(
            initialRoute: .activity, activate: {},
            revealExistingWindow: { true }, openWindow: { opens += 1 }
        )
        let center = DistributedNotificationCenter.default()
        coordinator.startListening(center: center)
        center.postNotificationName(
            ActivityRouteMessage.notificationName, object: nil,
            userInfo: ActivityRouteMessage.userInfo(for: .capacity),
            deliverImmediately: true
        )
        for _ in 0..<100 where coordinator.route != .capacity {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(coordinator.route == .capacity)
        #expect(opens == 0)
        coordinator.stopListening(center: center)
    }

    @MainActor
    @Test("Native hosting renders every route plus missing and no-match states at the minimum window size")
    func nativeRouteRendering() async throws {
        let fixture = try ActivityLedgerFixture()
        let preferences = ActivityPreferences(defaults: InMemoryActivityPreferencesStore())
        let store = ActivityViewStore(loader: ActivityDataLoader(
            databaseURL: fixture.databaseURL, visibilityURL: fixture.visibilityURL
        ), preferences: preferences)
        store.reload()
        try await wait(for: store)
        #expect(store.data != nil)

        for route in UsageDetailsRoute.allCases {
            let result = render(ActivityRootView(store: store, initialRoute: route))
            #expect(result.size.width == 900)
            #expect(result.size.height == 660)
            #expect(result.descendantCount > 4)
        }

        store.providerID = "not-present"
        store.reload()
        try await wait(for: store)
        #expect(store.data?.details.heatmapDays.isEmpty == true)
        let empty = render(ActivityRootView(store: store, initialRoute: .activity))
        #expect(empty.descendantCount > 4)

        let missingPreferences = ActivityPreferences(defaults: InMemoryActivityPreferencesStore())
        let missing = ActivityViewStore(loader: ActivityDataLoader(
            databaseURL: fixture.directory.appendingPathComponent("missing.sqlite"),
            visibilityURL: fixture.visibilityURL
        ), preferences: missingPreferences)
        missing.reload()
        try await wait(for: missing)
        #expect(missing.error == .databaseUnavailable)
        let failed = render(ActivityRootView(store: missing, initialRoute: .dataHealth))
        #expect(failed.descendantCount > 4)
        store.cancel()
        missing.cancel()
    }

    @MainActor
    @Test("Activity token metrics render both compact and expanded layouts")
    func nativeTokenMetricLayouts() async throws {
        let fixture = try ActivityLedgerFixture()
        let store = ActivityViewStore(loader: ActivityDataLoader(
            databaseURL: fixture.databaseURL, visibilityURL: fixture.visibilityURL
        ), preferences: ActivityPreferences(defaults: InMemoryActivityPreferencesStore()))
        store.reload()
        try await wait(for: store)
        let data = try #require(store.data)

        let compact = render(ActivityPage(store: store, data: data), size: .init(width: 560, height: 660))
        let expanded = render(ActivityPage(store: store, data: data), size: .init(width: 1_600, height: 660))

        #expect(compact.descendantCount > 4)
        #expect(expanded.descendantCount > 4)
        store.cancel()
    }

    @MainActor
    @Test("Native Capacity renders reset-aware changing and stale unchanged quota history")
    func nativeQuotaHistoryRendering() async throws {
        let fixture = try ActivityLedgerFixture()
        let now = Date()
        var calendar = Calendar.current
        calendar.timeZone = .current
        let components = calendar.dateComponents([.year, .month, .day], from: now)
        let today = try LocalDay(String(
            format: "%04d-%02d-%02d", components.year!, components.month!, components.day!
        ))
        try fixture.seedQuotaHistoryScenario(referenceDate: now, calendar: calendar)
        let loader = ActivityDataLoader(
            databaseURL: fixture.databaseURL, visibilityURL: fixture.visibilityURL,
            calendar: calendar, timeZone: calendar.timeZone
        )
        let loaded = try await loader.load(ActivityLoadRequest(
            period: .day, ending: today, providerIDs: [], modelIDs: []
        ))
        let chart = QuotaHistoryChartPresentation(points: loaded.quotaHistory)

        #expect(chart.changingSeries.count == 1)
        let changing = try #require(chart.changingSeries.first)
        #expect(changing.points.count == 3)
        #expect(Dictionary(grouping: changing.points, by: \.lineSegmentID).values.map(\.count).sorted() == [1, 2])
        #expect(chart.segmentMarkers.map(\.snapshotID).count == 2)
        #expect(chart.unchangedSeries.count == 1)
        #expect(chart.unchangedSeries.first?.points.allSatisfy(\.stale) == true)

        let preferences = ActivityPreferences(defaults: InMemoryActivityPreferencesStore())
        let store = ActivityViewStore(loader: loader, preferences: preferences)
        store.reload()
        try await wait(for: store)
        let result = render(ActivityRootView(store: store, initialRoute: .capacity))
        #expect(result.size == CGSize(width: 900, height: 660))
        #expect(result.descendantCount > 12)
        store.cancel()
    }

    private func request(providerID: String? = nil, modelID: String? = nil) -> ActivityLoadRequest {
        ActivityLoadRequest(
            period: .day, ending: try! LocalDay("2026-07-02"),
            providerIDs: providerID.map { [$0] } ?? [], modelIDs: modelID.map { [$0] } ?? []
        )
    }

    @MainActor
    private func wait(for store: ActivityViewStore) async throws {
        for _ in 0..<200 where store.isLoading {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(!store.isLoading)
    }

    @MainActor
    private func render<Content: View>(
        _ content: Content, size: CGSize = .init(width: 900, height: 660)
    ) -> (size: CGSize, descendantCount: Int) {
        let hosting = NSHostingView(rootView: content)
        hosting.frame = NSRect(origin: .zero, size: size)
        let window = NSWindow(
            contentRect: hosting.frame, styleMask: [.borderless],
            backing: .buffered, defer: false
        )
        window.contentView = hosting
        hosting.layoutSubtreeIfNeeded()
        hosting.displayIfNeeded()
        let result = (hosting.frame.size, descendants(of: hosting))
        RenderRetention.windows.append(window)
        return result
    }

    @MainActor
    private func descendants(of view: NSView) -> Int {
        view.subviews.reduce(view.subviews.count) { $0 + descendants(of: $1) }
    }
}

private final class MiniaturizedTestWindow: NSWindow {
    private var minimized = true
    private(set) var didMakeKeyAndOrderFront = false

    init() {
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: 900, height: 660),
            styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false
        )
    }

    override var isMiniaturized: Bool { minimized }

    override func deminiaturize(_ sender: Any?) {
        minimized = false
    }

    override func makeKeyAndOrderFront(_ sender: Any?) {
        didMakeKeyAndOrderFront = true
    }
}

private actor FailingActivityLoader: ActivityLoading {
    func load(_ request: ActivityLoadRequest) throws -> ActivityLoadedData {
        _ = request
        throw RepositoryError.databaseUnavailable
    }
}

private final class ActivityLedgerFixture: @unchecked Sendable {
    let directory: URL
    let databaseURL: URL
    let visibilityURL: URL

    init() throws {
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("OpenUsageActivityTests-\(UUID().uuidString)", isDirectory: true)
        databaseURL = directory.appendingPathComponent("activity.sqlite3")
        visibilityURL = directory.appendingPathComponent("visibility.json")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let script = #"""
        import sys
        from datetime import datetime, timezone
        from openusage_bar.activity_store import ActivityStore, DailyUsageRow, ProviderInstance
        store = ActivityStore(sys.argv[1])
        store.upsert_provider_instance(ProviderInstance(
            provider_id='codex', family_id='codex', display_name='Work Codex',
            category='subscription', credential_source='openusage', source_kind='openusage',
            observed_at='2026-07-02T23:59:00Z'))
        store.replace_daily_usage('codex', '2026-07-02', [DailyUsageRow(
            day='2026-07-02', provider_id='codex', model_id='gpt-5.5',
            input_tokens=4, output_tokens=2, cache_read_tokens=3,
            cache_creation_tokens=0, reasoning_tokens=None, total_tokens=9,
            cost_amount='1.25', cost_currency='USD', cost_basis='exact',
            quality='exact', imported_at='2026-07-02T23:59:00Z')],
            imported_at='2026-07-02T23:59:00Z')
        store.record_source_success(
            'codex', 'openusage.daily', datetime(2026, 7, 2, 23, 59, tzinfo=timezone.utc),
            freshness_seconds=300)
        store.close()
        """#
        let process = Process()
        process.executableURL = root.appendingPathComponent(".build-venv/bin/python")
        process.arguments = ["-c", script, databaseURL.path]
        process.currentDirectoryURL = root
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = root.path
        process.environment = environment
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { throw FixtureFailure.creation }
    }

    func seedQuotaHistoryScenario(referenceDate: Date, calendar: Calendar) throws {
        var database: OpaquePointer?
        guard sqlite3_open(databaseURL.path, &database) == SQLITE_OK, let database else {
            throw FixtureFailure.creation
        }
        defer { sqlite3_close(database) }
        let start = calendar.startOfDay(for: referenceDate)
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        func timestamp(hour: Int) throws -> String {
            guard let date = calendar.date(byAdding: .hour, value: hour, to: start) else {
                throw FixtureFailure.creation
            }
            return formatter.string(from: date)
        }
        let observed1 = try timestamp(hour: 9)
        let observed2 = try timestamp(hour: 10)
        let observed3 = try timestamp(hour: 11)
        let reset1 = try timestamp(hour: 12)
        let reset2 = try timestamp(hour: 16)
        let monthlyReset = try timestamp(hour: 20)
        let sql = """
        INSERT INTO quota_snapshots(
          record_id,observed_at,provider_id,account_ref,quota_name,payload_json,payload_hash
        ) VALUES
          ('codex.weekly','\(observed1)','codex','','weekly',
           '{"remaining_ratio":0.82,"resets_at":"\(reset1)","state":"ok","stale":false}','weekly-1'),
          ('codex.weekly','\(observed2)','codex','','weekly',
           '{"remaining_ratio":0.55,"resets_at":"\(reset1)","state":"ok","stale":false}','weekly-2'),
          ('codex.weekly','\(observed3)','codex','','weekly',
           '{"remaining_ratio":0.99,"resets_at":"\(reset2)","state":"ok","stale":false}','weekly-3'),
          ('codex.monthly','\(observed1)','codex','','monthly',
           '{"remaining_ratio":1.0,"resets_at":"\(monthlyReset)","state":"ok","stale":true}','monthly-1'),
          ('codex.monthly','\(observed2)','codex','','monthly',
           '{"remaining_ratio":1.0,"resets_at":"\(monthlyReset)","state":"ok","stale":true}','monthly-2');
        """
        guard sqlite3_exec(database, sql, nil, nil, nil) == SQLITE_OK else {
            throw FixtureFailure.creation
        }
    }

    deinit { try? FileManager.default.removeItem(at: directory) }
}

private enum FixtureFailure: Error { case creation }

private final class RevisionMutation: @unchecked Sendable {
    private let databaseURL: URL
    private let phase: ActivityLoadPhase
    private let lock = NSLock()
    private var didMutate = false
    private var phaseCalls: [ActivityLoadPhase: Int] = [:]

    init(databaseURL: URL, phase: ActivityLoadPhase) {
        self.databaseURL = databaseURL
        self.phase = phase
    }

    func call(_ current: ActivityLoadPhase, _ attempt: Int) throws {
        lock.lock()
        defer { lock.unlock() }
        phaseCalls[current, default: 0] += 1
        guard current == phase, attempt == 0, !didMutate else { return }
        didMutate = true
        var database: OpaquePointer?
        guard sqlite3_open(databaseURL.path, &database) == SQLITE_OK, let database else {
            throw FixtureFailure.creation
        }
        defer { sqlite3_close(database) }
        let sql = """
        INSERT INTO quota_snapshots(
          record_id,observed_at,provider_id,account_ref,quota_name,payload_json,payload_hash
        ) VALUES(
          'codex.weekly','2026-07-02T12:00:00.000000Z','codex','','weekly',
          '{"remaining_ratio":0.75,"state":"ok","stale":false}','revision-quota'
        );
        INSERT INTO change_log(
          record_type,record_id,revision,operation,changed_at,payload_json,payload_hash
        ) VALUES(
          'quota','codex.weekly',999999,'update','2026-07-02T12:00:00.000000Z',NULL,'revision-change'
        );
        """
        guard sqlite3_exec(database, sql, nil, nil, nil) == SQLITE_OK else {
            throw FixtureFailure.creation
        }
    }

    func calls(for phase: ActivityLoadPhase) -> Int {
        lock.withLock { phaseCalls[phase, default: 0] }
    }
}

@MainActor
private enum RenderRetention {
    static var windows: [NSWindow] = []
}
