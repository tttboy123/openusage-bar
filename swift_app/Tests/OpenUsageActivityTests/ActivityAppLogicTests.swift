import AppKit
import Foundation
import Testing
@testable import OpenUsageActivity
@testable import UsageCore

@Suite("Usage Details app contracts")
struct ActivityAppLogicTests {
    private func day(_ value: String) -> LocalDay { try! LocalDay(value) }

    @MainActor
    @Test("Usage Details exits after its last window closes")
    func terminateAfterLastWindow() {
        let delegate = ActivityAppDelegate()
        #expect(delegate.applicationShouldTerminateAfterLastWindowClosed(NSApplication.shared))
    }

    @Test("Isolated preferences stay in memory and create no preference files")
    func isolatedPreferencesCleanup() {
        let before = testPreferenceFiles()
        let store = InMemoryActivityPreferencesStore()
        let preferences = ActivityPreferences(defaults: store)
        preferences.save(.init(period: .week, providerID: "codex", modelID: nil))

        #expect(preferences.load() == .init(period: .week, providerID: "codex", modelID: nil))
        #expect(testPreferenceFiles() == before)
    }

    private func testPreferenceFiles() -> Set<String> {
        let directory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Preferences", isDirectory: true)
        return Set((try? FileManager.default.contentsOfDirectory(
            at: directory, includingPropertiesForKeys: nil
        ))?.map(\.lastPathComponent).filter {
            $0.hasPrefix("OpenUsageActivityTests.") && $0.hasSuffix(".plist")
        } ?? [])
    }

    @Test("Every filter load uses one bounded annual canonical dataset")
    func boundedLoadRequest() {
        let request = ActivityLoadRequest(
            period: .week, ending: day("2026-07-14"),
            providerIDs: ["codex"], modelIDs: ["gpt-5.5"]
        )
        #expect(request.repositoryRange == day("2025-07-15")...day("2026-07-14"))
        #expect(request.metricRange == day("2026-07-08")...day("2026-07-14"))
        #expect(request.repositoryRange.dayCount == 365)
        #expect(request.providerIDs == ["codex"])
    }

    @Test("Provider edits resolve the sibling settings executable")
    func providerEditCommandResolution() throws {
        let activity = URL(fileURLWithPath: "/Applications/OpenUsage Bar.app/Contents/Helpers/OpenUsage Activity.app")
        let expected = URL(fileURLWithPath: "/Applications/OpenUsage Bar.app/Contents/Helpers/OpenUsage Provider Settings.app/Contents/MacOS/OpenUsage Provider Settings")
        let command = try #require(ProviderMutationCommand.resolve(
            activityBundleURL: activity,
            activityExecutableURL: activity.appendingPathComponent("Contents/MacOS/OpenUsage Activity"),
            isExecutable: { $0 == expected }
        ))

        #expect(command.executableURL == expected)
        #expect(command.arguments == ["provider-mutate"])
    }

    @Test("Provider edit wire payload is scoped and does not contain site or endpoint")
    func providerEditWirePayload() throws {
        let request = ProviderEditRequest(
            providerID: "step-plan-main", name: "Main",
            apiKey: "replacement", sessionCookie: ""
        )
        let object = try #require(
            JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any]
        )

        #expect(object["version"] as? Int == 1)
        #expect(object["action"] as? String == "update_connection")
        #expect(object["providerId"] as? String == "step-plan-main")
        #expect(object["site"] == nil)
        #expect(object["endpoint"] == nil)
    }

    @Test("Provider mutation helper is terminated at its time bound")
    func providerMutationTimeout() async {
        let client = ProviderMutationClient(limits: .init(
            timeout: .milliseconds(50), maximumResponseBytes: 1_024
        ))
        let start = ContinuousClock.now
        let result = await client.submit(
            providerEditRequest(),
            command: .init(
                executableURL: URL(fileURLWithPath: "/bin/sleep"), arguments: ["5"]
            )
        )

        #expect(result == .failure(.timedOut))
        #expect(start.duration(to: .now) < .seconds(2))
    }

    @Test("Provider mutation helper rejects oversized output")
    func providerMutationOversizedOutput() async {
        let result = await mutationClient(maximumResponseBytes: 64).submit(
            providerEditRequest(),
            command: shellCommand("head -c 65 /dev/zero")
        )
        #expect(result == .failure(.responseTooLarge))
    }

    @Test("Provider mutation helper rejects invalid UTF-8")
    func providerMutationInvalidUTF8() async {
        let result = await mutationClient().submit(
            providerEditRequest(),
            command: shellCommand("printf '\\377'")
        )
        #expect(result == .failure(.invalidResponse))
    }

    @Test("Provider mutation helper sanitizes nonzero and truncated responses")
    func providerMutationFailures() async {
        let nonzero = await mutationClient().submit(
            providerEditRequest(),
            command: .init(executableURL: URL(fileURLWithPath: "/usr/bin/false"), arguments: [])
        )
        let truncated = await mutationClient().submit(
            providerEditRequest(),
            command: shellCommand("printf '{\\\"version\\\":1'")
        )

        #expect(nonzero == .failure(.couldNotLaunch))
        #expect(truncated == .failure(.invalidResponse))
    }

    @Test("Provider mutation helper decodes one bounded successful response")
    func providerMutationSuccess() async {
        let result = await mutationClient().submit(
            providerEditRequest(),
            command: shellCommand(
                "printf '{\\\"version\\\":1,\\\"ok\\\":true,\\\"message\\\":\\\"Saved\\\"}'"
            )
        )

        #expect(result == .success(.init(version: 1, ok: true, message: "Saved")))
    }

    private func mutationClient(maximumResponseBytes: Int = 1_024) -> ProviderMutationClient {
        ProviderMutationClient(limits: .init(
            timeout: .seconds(1), maximumResponseBytes: maximumResponseBytes
        ))
    }

    private func providerEditRequest() -> ProviderEditRequest {
        ProviderEditRequest(
            providerID: "step-plan-main", name: "Main",
            apiKey: "replacement", sessionCookie: ""
        )
    }

    private func shellCommand(_ script: String) -> ProviderMutationCommand {
        ProviderMutationCommand(
            executableURL: URL(fileURLWithPath: "/bin/sh"), arguments: ["-c", script]
        )
    }

    @Test("Provider modification stays in the selected detail pane")
    func providerModificationIsInline() throws {
        let source = try String(
            contentsOf: URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
                .appendingPathComponent("Sources/OpenUsageActivity/ProviderCenterViews.swift"),
            encoding: .utf8
        )
        let detail = try #require(source.range(of: "private struct ProviderConnectionDetail"))
        let nextPage = try #require(source.range(of: "private struct ProviderDetailSection"))
        let section = String(source[detail.lowerBound..<nextPage.lowerBound])

        #expect(section.contains("Edit Connection"))
        #expect(section.contains(".buttonStyle(.borderedProminent)"))
        #expect(section.contains(".tint(.accentColor)"))
        #expect(section.contains("SecureField"))
        #expect(section.contains("ProviderMutationService.submit"))
        #expect(!section.contains("Button(\"Open Provider Settings\""))
    }

    @Test("Configured connections remain editable before a successful collection")
    func configuredProviderConnections() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("providers.json")
        try Data(#"""
        {
          "version": 2,
          "providers": [
            {"provider_id":"step-plan-main","name":"Main","type":"step_plan","site":"china"},
            {"provider_id":"feed-zai","name":"ZAI Feed","type":"daily_usage_feed","family_id":"zai","endpoint":"https://example.com"},
            {"provider_id":"cost-openai","name":"OpenAI Cost","type":"daily_cost_feed","family_id":"openai","endpoint":"https://example.com"}
          ]
        }
        """#.utf8).write(to: url)

        let connections = try ProviderConnectionSummaryStore(url: url).load()

        #expect(connections.map(\.providerID) == ["step-plan-main", "feed-zai", "cost-openai"])
        #expect(connections[0].familyID == "step_plan")
        #expect(connections[0].site == "china")
        #expect(connections[0].isStepPlan)
        #expect(connections[0].isManaged)
        #expect(connections[1].familyID == "zai")
        #expect(!connections[1].isStepPlan)
        #expect(connections[1].isManaged)
        #expect(connections[1].credentialLabel == "Replacement API key")
        #expect(connections[2].familyID == "openai")
        #expect(connections[2].kind == "daily_cost_feed")
    }

    @Test("Stale background loads cannot publish over a newer filter")
    func loadGeneration() {
        var gate = ActivityLoadGate()
        let first = gate.begin()
        let second = gate.begin()
        #expect(!gate.canPublish(first))
        #expect(gate.canPublish(second))
        gate.cancel()
        #expect(!gate.canPublish(second))
    }

    @Test("Heatmap geometry is fixed and copy has no long dash glyphs")
    func geometryAndCopy() {
        #expect(HeatmapGeometry.cellSize == 13)
        #expect(HeatmapGeometry.rowCount == 7)
        #expect(HeatmapGeometry.spacing == 4)
        #expect(DetailsCopy.visibleText.allSatisfy { !$0.contains("—") && !$0.contains("–") })
        #expect(DetailsCopy.sidebar == [
            "Activity", "Capacity", "API Spend", "Local Tools",
            "Providers", "Data Health", "Automation",
        ])
        #expect(!DetailsCopy.visibleText.contains("Longest Task"))
        #expect(!DetailsCopy.visibleText.contains("Unavailable"))
        #expect(DetailsCopy.visibleText.contains("Active Days"))
    }

    @Test("Automation opens without the ledger and loads it only when leaving")
    func automationLedgerPolicy() {
        #expect(!ActivityRouteLoadingPolicy.loadsLedgerOnAppear(.automation))
        #expect(ActivityRouteLoadingPolicy.loadsLedgerOnAppear(.activity))
        #expect(ActivityRouteLoadingPolicy.loadsLedgerAfterSelection(
            from: .automation, to: .capacity
        ))
        #expect(!ActivityRouteLoadingPolicy.loadsLedgerAfterSelection(
            from: .automation, to: .automation
        ))
        #expect(!ActivityRouteLoadingPolicy.loadsLedgerAfterSelection(
            from: .activity, to: .automation
        ))
    }

    @Test("Dynamic product labels cover every localized enum state")
    func localizedDynamicLabels() {
        #expect(UsagePeriod.allCases.map(\.title) == ["Day", "Week", "Month", "Year"])
        #expect([
            ProviderProductCategory.subscription,
            .api,
            .localTool,
        ].map(\.title) == ["Subscription", "Api", "Local Tool"])
        #expect([
            ProviderMetricFamily.subscriptionQuota,
            .tokenActivity,
            .billing,
            .operational,
        ].map(\.title) == ["Subscription Quota", "Token Activity", "Billing", "Operational"])
        #expect([
            CredentialSourceType.none,
            .keychain,
            .browserSession,
            .apiKey,
            .oauth,
            .cli,
            .local,
        ].map(\.title) == [
            "Provider owned", "Keychain", "Browser Session", "API Key", "OAuth", "CLI", "Local",
        ])
        #expect([
            APISpendQuality.reported,
            .estimated,
            .partial,
        ].map(\.title) == ["Reported", "Estimated", "Observed, partial"])
        #expect([
            ProviderMutationFailure.unavailable,
            .couldNotLaunch,
            .timedOut,
            .responseTooLarge,
            .invalidResponse,
        ].map(\.message).allSatisfy { !$0.isEmpty })
        #expect(DateText.reset(nil) == "Reset unavailable")
        #expect(CapacityText.value(CapacityItem(
            recordID: "unknown", providerID: "codex", accountRef: "",
            quotaName: "weekly", unit: "percent", used: nil, limit: nil,
            remaining: nil, remainingRatio: nil, resetsAt: nil,
            periodStart: nil, periodEnd: nil, observedAt: "2026-07-18T00:00:00Z",
            freshnessSeconds: 0, state: "unknown", quality: "unknown",
            stale: false, revision: 1, sourceID: "current.quota", quotaWindow: "weekly"
        )) == "Unavailable")
    }

    @Test("Annual heatmap opens at the latest real day")
    func heatmapLatestScrollTarget() throws {
        let range = day("2025-07-15")...day("2026-07-14")
        let details = range.days.map { value in
            HeatmapDayDetail(
                activity: ActivityDay(
                    day: value, state: .coveredZero, totalTokens: 0,
                    observedTokens: 0, heatLevel: 0
                ),
                quality: .exact, lastCollectionAt: nil
            )
        }
        let layout = HeatmapCalendarLayout(range: range, details: details)

        #expect(HeatmapScrollTarget.latestPosition(in: layout) == 366)
        #expect(layout.slots[366].detail?.activity.day == day("2026-07-14"))
    }

    @Test("Heatmap hover text exposes date Token value and collection quality")
    func heatmapHoverText() {
        let active = HeatmapDayDetail(
            activity: ActivityDay(
                day: day("2026-07-14"), state: .coveredActive,
                totalTokens: 74_200_000, observedTokens: 74_200_000, heatLevel: 5
            ),
            quality: .estimated, lastCollectionAt: "2026-07-14T23:59:00Z", isStale: true
        )
        let text = HeatmapTooltipText(active)

        #expect(text.title == "2026-07-14")
        #expect(text.value == "74.2M Tokens")
        #expect(text.metadata.contains("Estimated"))
        #expect(text.metadata.contains("Stale"))
        #expect(text.accessibilityValue.contains("74.2M Tokens"))

        let missing = HeatmapTooltipText(HeatmapDayDetail(
            activity: ActivityDay(
                day: day("2026-07-13"), state: .missing,
                totalTokens: nil, observedTokens: 0, heatLevel: nil
            ),
            quality: .missing, lastCollectionAt: nil
        ))
        #expect(missing.value == "No collection data")
    }

    @Test("Heatmap pointer resolves one square and ignores spacing")
    func heatmapPointerTarget() {
        #expect(HeatmapPointerTarget.position(x: 1, y: 1, slotCount: 14) == 0)
        #expect(HeatmapPointerTarget.position(x: 18, y: 1, slotCount: 14) == 7)
        #expect(HeatmapPointerTarget.position(x: 1, y: 18, slotCount: 14) == 1)
        #expect(HeatmapPointerTarget.position(x: 14, y: 1, slotCount: 14) == nil)
        #expect(HeatmapPointerTarget.position(x: 1, y: 14, slotCount: 14) == nil)
        #expect(HeatmapPointerTarget.position(x: -1, y: 1, slotCount: 14) == nil)
        #expect(HeatmapPointerTarget.position(x: 35, y: 1, slotCount: 14) == nil)
    }

    @Test("Heatmap inspection prefers hover and falls back to keyboard selection")
    func heatmapInspectionSelection() {
        let first = HeatmapDayDetail(
            activity: ActivityDay(
                day: day("2026-07-13"), state: .coveredActive,
                totalTokens: 1, observedTokens: 1, heatLevel: 1
            ), quality: .exact, lastCollectionAt: "2026-07-13T10:00:00Z"
        )
        let second = HeatmapDayDetail(
            activity: ActivityDay(
                day: day("2026-07-14"), state: .coveredActive,
                totalTokens: 2, observedTokens: 2, heatLevel: 2
            ), quality: .exact, lastCollectionAt: "2026-07-14T10:00:00Z"
        )
        #expect(HeatmapInspectionSelection.visible(
            hovered: first, selected: second.activity.day, in: [first, second]
        ) == first)
        #expect(HeatmapInspectionSelection.visible(
            hovered: nil, selected: second.activity.day, in: [first, second]
        ) == second)
        #expect(HeatmapInspectionSelection.visible(
            hovered: nil, selected: nil, in: [first, second]
        ) == nil)
    }

    @Test("Heatmap is one grouped accessibility stop and metrics omit task duration")
    func sourceAccessibilityAndMetricContract() throws {
        let source = try String(
            contentsOf: URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
                .appendingPathComponent("Sources/OpenUsageActivity/ActivityDashboardViews.swift"),
            encoding: .utf8
        )
        let heatmap = try #require(source.range(of: "private struct HeatmapSection"))
        let cell = try #require(source.range(of: "private struct HeatmapCell"))
        let section = String(source[heatmap.lowerBound..<cell.lowerBound])
        #expect(section.contains(".accessibilityElement(children: .ignore)"))
        #expect(section.contains(".accessibilityHidden(true)"))
        #expect(section.components(separatedBy: ".focusable()").count - 1 == 1)
        #expect(section.contains("Text(\"Partial\")"))
        #expect(section.contains("ScrollViewReader"))
        #expect(section.contains(".scrollTo("))
        #expect(section.contains(".onContinuousHover"))
        let metricStart = try #require(source.range(of: "private struct MetricStrip"))
        let metricEnd = try #require(source.range(of: "private struct MetricValue"))
        let metrics = String(source[metricStart.lowerBound..<metricEnd.lowerBound])
        #expect(metrics.contains("Active Days"))
        #expect(metrics.contains("Observed Peak"))
        #expect(metrics.contains("Observed Active Days"))
        #expect(!metrics.contains("Longest Task"))
    }

    @Test("Visibility preserves hidden Claude Code and fails open on malformed data")
    func visibility() throws {
        let valid = Data(#"{"version":1,"hidden_provider_ids":["claude_code"]}"#.utf8)
        #expect(try VisibilityStore.decode(valid).hiddenProviderIDs == ["claude_code"])
        #expect(throws: VisibilityStoreError.self) {
            try VisibilityStore.decode(Data(#"{"version":1,"hidden_provider_ids":["../secret"]}"#.utf8))
        }
    }

    @Test("Credential management helper launch is direct argv and route free")
    func settingsHelperPlan() {
        let activity = URL(fileURLWithPath: "/Applications/OpenUsage Bar.app/Contents/Helpers/OpenUsage Activity.app")
        let plan = ActivityHelperPlan.settings(
            activityBundleURL: activity,
            activityExecutableURL: activity.appendingPathComponent("Contents/MacOS/OpenUsageActivity"),
            exists: { $0.lastPathComponent == "OpenUsage Provider Settings.app" }
        )
        #expect(plan?.target == .application(activity.deletingLastPathComponent().appendingPathComponent("OpenUsage Provider Settings.app")))
        #expect(plan?.arguments == [])
        #expect(String(describing: plan).lowercased().contains("token") == false)
        #expect(String(describing: plan).lowercased().contains("cookie") == false)
    }

    @MainActor
    @Test("Development settings fallback is a typed direct argv launch")
    func settingsExecutableFallback() async throws {
        let executable = URL(fileURLWithPath: "/repo/swift_app/.build/debug/OpenUsageActivity")
        let settings = executable.deletingLastPathComponent().appendingPathComponent("OpenUsageSettings")
        let plan = try #require(ActivityHelperPlan.settings(
            activityBundleURL: executable.deletingLastPathComponent(),
            activityExecutableURL: executable,
            exists: { _ in false }, isExecutable: { $0 == settings }
        ))
        var invocation: (URL, [String])?
        let service = ActivityHelperLaunchService(
            openApplication: { _, _, completion in completion(nil) },
            runExecutable: { url, arguments in invocation = (url, arguments); return true }
        )
        #expect(await service.launch(plan) == .launched)
        #expect(invocation?.0 == settings)
        #expect(invocation?.1 == [])
    }

    @Test("API spend prefers native costs, keeps uncovered legacy scopes, and labels incomplete facts")
    func selectedSpend() throws {
        let inside = usage(day: "2026-07-14", cost: "1.25", basis: "exact")
        let outside = usage(day: "2026-07-13", cost: "99.00", basis: "exact")
        let native = DailyCostDataset(
            records: [dailyCost(providerID: "openai", amount: "2.50")],
            coverage: [CostCoverageDay(
                day: day("2026-07-14"), providerID: "openai", accountRef: "",
                isCovered: true
            )],
            knownScopes: [ProviderScope(providerID: "openai", accountRef: "")],
            revision: 1
        )
        let exact = APISpendAggregator.make(
            costs: native, legacyRecords: [inside, outside],
            range: day("2026-07-14")...day("2026-07-14"),
            isLegacyCoverageComplete: true
        )
        #expect(exact.totals.map(\.amount) == [Decimal(string: "3.75")!])
        #expect(exact.totals.map(\.quality) == [.reported])

        let replacement = DailyCostDataset(
            records: [dailyCost(providerID: "codex", amount: "4")],
            coverage: [CostCoverageDay(
                day: day("2026-07-14"), providerID: "codex", accountRef: "",
                isCovered: true
            )],
            knownScopes: [ProviderScope(providerID: "codex", accountRef: "")],
            revision: 1
        )
        let replaced = APISpendAggregator.make(
            costs: replacement, legacyRecords: [inside],
            range: day("2026-07-14")...day("2026-07-14"),
            isLegacyCoverageComplete: true
        )
        #expect(replaced.totals.map(\.amount) == [Decimal(string: "4")!])

        let incomplete = DailyCostDataset(
            records: native.records,
            coverage: [CostCoverageDay(
                day: day("2026-07-14"), providerID: "openai", accountRef: "",
                isCovered: false
            )],
            knownScopes: native.knownScopes,
            revision: 1
        )
        let partial = APISpendAggregator.make(
            costs: incomplete, legacyRecords: [inside],
            range: day("2026-07-14")...day("2026-07-14"),
            isLegacyCoverageComplete: true
        )
        #expect(partial.totals.map(\.quality) == [.partial])

        let unavailable = APISpendAggregator.make(
            costs: .init(records: [], coverage: [], knownScopes: [], revision: 1),
            legacyRecords: [usage(day: "2026-07-14", cost: nil, basis: nil)],
            range: day("2026-07-14")...day("2026-07-14"),
            isLegacyCoverageComplete: true
        )
        #expect(unavailable.totals.isEmpty)
        #expect(unavailable.coverage == .missing)

        let knownZero = APISpendAggregator.make(
            costs: .init(
                records: [],
                coverage: [CostCoverageDay(
                    day: day("2026-07-14"), providerID: "openai", accountRef: "",
                    isCovered: true
                )],
                knownScopes: [ProviderScope(providerID: "openai", accountRef: "")],
                revision: 1
            ),
            legacyRecords: [], range: day("2026-07-14")...day("2026-07-14"),
            isLegacyCoverageComplete: true
        )
        #expect(knownZero.totals.isEmpty)
        #expect(knownZero.coverage == .complete)

        let partialZero = APISpendAggregator.make(
            costs: .init(
                records: [],
                coverage: [
                    CostCoverageDay(
                        day: day("2026-07-14"), providerID: "openai", accountRef: "",
                        isCovered: true
                    ),
                    CostCoverageDay(
                        day: day("2026-07-13"), providerID: "openai", accountRef: "",
                        isCovered: false
                    ),
                ],
                knownScopes: [ProviderScope(providerID: "openai", accountRef: "")],
                revision: 1
            ),
            legacyRecords: [], range: day("2026-07-13")...day("2026-07-14"),
            isLegacyCoverageComplete: true
        )
        #expect(partialZero.totals.isEmpty)
        #expect(partialZero.coverage == .partial)

        let derivedNative = APISpendAggregator.make(
            costs: .init(
                records: [dailyCost(
                    providerID: "openai", amount: "2", quality: "derived"
                )],
                coverage: [CostCoverageDay(
                    day: day("2026-07-14"), providerID: "openai", accountRef: "",
                    isCovered: true
                )],
                knownScopes: [ProviderScope(providerID: "openai", accountRef: "")],
                revision: 1
            ),
            legacyRecords: [], range: day("2026-07-14")...day("2026-07-14"),
            isLegacyCoverageComplete: true
        )
        #expect(derivedNative.totals.map(\.quality) == [.estimated])
    }

    @Test("Local tools expose period usage and annual last activity instead of identity-only rows")
    func localToolUsageSummaries() throws {
        let period = [
            usage(
                day: "2026-07-14", provider: "hermes", model: "deepseek-chat",
                tokens: 17_325, quality: "derived"
            ),
            usage(
                day: "2026-07-16", provider: "hermes", model: "MiniMax-M3",
                tokens: 3_262, quality: "derived"
            ),
        ]
        let history = period + [usage(
            day: "2026-06-22", provider: "openclaw", model: "deepseek-chat",
            tokens: 47_021, quality: "derived"
        )]
        let health = ["hermes", "openclaw"].map {
            SourceHealthItem(
                providerID: $0, sourceID: "openusage.daily", state: "ok",
                effectiveState: "ok", lastAttemptAt: "2026-07-16T13:29:49Z",
                lastSuccessAt: "2026-07-16T13:29:49Z", staleAt: nil, errorCode: nil
            )
        }
        let summaries = LocalToolUsagePresentation.make(
            descriptors: [
                ProviderCatalog.descriptor(for: "hermes"),
                ProviderCatalog.descriptor(for: "openclaw"),
            ],
            periodRecords: period, historyRecords: history, health: health
        )

        let hermes = try #require(summaries.first { $0.providerID == "hermes" })
        #expect(hermes.observedTokens == 20_587)
        #expect(hermes.activeDays == 2)
        #expect(hermes.knownModelIDs == ["MiniMax-M3", "deepseek-chat"])
        #expect(hermes.lastActivityDay == day("2026-07-16"))
        #expect(hermes.quality == .estimated)
        #expect(hermes.state == "ok")

        let openClaw = try #require(summaries.first { $0.providerID == "openclaw" })
        #expect(openClaw.observedTokens == 0)
        #expect(openClaw.activeDays == 0)
        #expect(openClaw.knownModelIDs == ["deepseek-chat"])
        #expect(openClaw.lastActivityDay == day("2026-06-22"))
        #expect(openClaw.lastCollectionAt == "2026-07-16T13:29:49Z")
    }

    @Test("Persisted filters restore in an isolated defaults suite and corrupt values fail safe")
    func persistedFilters() throws {
        let defaults = InMemoryActivityPreferencesStore()
        let preferences = ActivityPreferences(defaults: defaults)
        preferences.save(.init(period: .month, providerID: "codex", modelID: "gpt-5.5"))
        #expect(preferences.load() == .init(period: .month, providerID: "codex", modelID: "gpt-5.5"))
        defaults.set("century", forKey: ActivityPreferences.periodKey)
        defaults.set("../secret", forKey: ActivityPreferences.providerKey)
        #expect(preferences.load() == .init(period: .year, providerID: nil, modelID: "gpt-5.5"))
        preferences.clearFilters()
        #expect(preferences.load() == .init(period: .year, providerID: nil, modelID: nil))
    }

    private func usage(day value: String, cost: String?, basis: String?) -> DailyUsage {
        usage(
            day: value, provider: "codex", model: "gpt-5.5", tokens: 1,
            quality: "exact", cost: cost, basis: basis
        )
    }

    private func usage(
        day value: String, provider: String, model: String, tokens: Int64,
        quality: String, cost: String? = nil, basis: String? = nil
    ) -> DailyUsage {
        DailyUsage(
            day: day(value), providerID: provider, accountRef: "", modelID: model,
            inputTokens: tokens, outputTokens: 0, cacheReadTokens: 0, cacheCreationTokens: 0,
            reasoningTokens: nil, totalTokens: tokens, costAmount: cost,
            costCurrency: cost == nil ? nil : "USD", costBasis: basis, quality: quality,
            importedAt: "\(value)T23:59:00Z", revision: 1,
            recordID: "\(value).\(provider).\(model)"
        )
    }

    private func dailyCost(
        providerID: String, amount: String, basis: String = "provider_reported",
        quality: String = "direct"
    ) -> DailyCost {
        DailyCost(
            day: day("2026-07-14"), providerID: providerID, accountRef: "",
            costKind: "actual", currency: "USD", amount: amount,
            basis: basis, quality: quality, importedAt: "2026-07-15T00:05:00Z",
            revision: 1,
            recordID: "cost:2026-07-14:\(providerID)::actual:USD"
        )
    }

    @Test("Chart focus clears for every bounded lifecycle event")
    func focusClearing() {
        for reason in ChartFocusClearReason.allCases {
            var focus = ChartFocus(
                day: day("2026-07-02"), source: .pointer,
                pointerAnchor: ChartAnchor(x: 20, y: 30)
            )
            focus.clear(reason)
            #expect(focus.day == nil)
            #expect(focus.source == nil)
            #expect(focus.pointerAnchor == nil)
        }
    }

    @Test("Pointer and keyboard focus keep distinct anchors and same-day pointer movement stays current")
    func chartFocusSources() {
        var focus = ChartFocus(day: nil)
        focus.selectPointer(day: day("2026-07-02"), anchor: ChartAnchor(x: 20, y: 30))
        #expect(focus.day == day("2026-07-02"))
        #expect(focus.source == .pointer)
        #expect(focus.pointerAnchor == ChartAnchor(x: 20, y: 30))

        focus.selectPointer(day: day("2026-07-02"), anchor: ChartAnchor(x: 80, y: 90))
        #expect(focus.pointerAnchor == ChartAnchor(x: 80, y: 90))

        focus.selectKeyboard(day: day("2026-07-03"))
        #expect(focus.day == day("2026-07-03"))
        #expect(focus.source == .keyboard)
        #expect(focus.pointerAnchor == nil)
    }

    @Test("Tooltip placement flips near right and top edges then clamps every corner")
    func tooltipPlacementBoundaries() {
        let container = ChartSize(width: 300, height: 200)
        let tooltip = ChartSize(width: 80, height: 40)
        #expect(TooltipPlacement.center(
            anchor: ChartAnchor(x: 100, y: 100), tooltip: tooltip, container: container
        ) == ChartAnchor(x: 152, y: 68))
        #expect(TooltipPlacement.center(
            anchor: ChartAnchor(x: 280, y: 100), tooltip: tooltip, container: container
        ) == ChartAnchor(x: 228, y: 68))
        #expect(TooltipPlacement.center(
            anchor: ChartAnchor(x: 100, y: 10), tooltip: tooltip, container: container
        ) == ChartAnchor(x: 152, y: 42))

        for anchor in [
            ChartAnchor(x: 0, y: 0), ChartAnchor(x: 300, y: 0),
            ChartAnchor(x: 0, y: 200), ChartAnchor(x: 300, y: 200),
        ] {
            let center = TooltipPlacement.center(
                anchor: anchor, tooltip: tooltip, container: container
            )
            #expect((48...252).contains(center.x))
            #expect((28...172).contains(center.y))
        }
    }

    @Test("Oversized tooltip returns one finite center inside the container")
    func oversizedTooltipPlacement() {
        let center = TooltipPlacement.center(
            anchor: ChartAnchor(x: 95, y: 5),
            tooltip: ChartSize(width: 500, height: 400),
            container: ChartSize(width: 100, height: 80)
        )
        #expect(center == ChartAnchor(x: 50, y: 40))
        #expect(center.x.isFinite)
        #expect(center.y.isFinite)
    }

    @Test("Quota tooltip placement keeps its real minimum size before measurement arrives")
    func quotaTooltipPlacementSize() {
        #expect(QuotaHistoryTooltipGeometry.placementSize(
            measured: ChartSize(width: 0, height: 0)
        ) == ChartSize(width: 220, height: 112))
        #expect(QuotaHistoryTooltipGeometry.placementSize(
            measured: ChartSize(width: 260, height: 170)
        ) == ChartSize(width: 260, height: 170))
    }

    @Test("Quota series visual identity is stable and bounded")
    func quotaVisualIdentity() {
        let firstColor = QuotaHistoryVisualStyle.colorIndex(styleKey: "Series abcdef01")
        let repeatedColor = QuotaHistoryVisualStyle.colorIndex(styleKey: "Series abcdef01")
        let collidingColor = QuotaHistoryVisualStyle.colorIndex(styleKey: "Series abcdef09")
        let firstDash = QuotaHistoryVisualStyle.dashIndex(styleKey: "Series abcdef01")
        let collidingDash = QuotaHistoryVisualStyle.dashIndex(styleKey: "Series abcdef09")

        #expect(firstColor == repeatedColor)
        #expect(firstColor == collidingColor)
        #expect(firstDash != collidingDash)
        #expect((0..<8).contains(firstColor))
        #expect((0..<8).contains(firstDash))
    }

    @Test("Quota history limits the default plot and keeps the lowest remaining series")
    func quotaHistoryDefaultVisibility() {
        let points = (0..<8).map { index in
            quotaPoint(
                snapshotID: Int64(index), observedAt: TimeInterval(index),
                ratio: Double(8 - index) / 10,
                seriesID: "series-\(index)", seriesLabel: "Provider \(index)"
            )
        }
        let chart = QuotaHistoryChartPresentation(points: points)

        #expect(QuotaHistorySeriesVisibility.defaultVisibleIDs(
            in: chart.series, maxVisible: 6
        ) == Set(["series-2", "series-3", "series-4", "series-5", "series-6", "series-7"]))
        #expect(QuotaHistorySeriesVisibility.defaultVisibleIDs(
            in: chart.series, maxVisible: 20
        ) == Set(points.map(\.seriesID)))
    }

    @Test("Quota history legend toggles series without hiding the final visible line")
    func quotaHistoryVisibilityToggle() {
        let all = Set(["alpha", "beta", "gamma"])
        #expect(QuotaHistorySeriesVisibility.toggled(
            "beta", visible: all, allSeries: all
        ) == Set(["alpha", "gamma"]))
        #expect(QuotaHistorySeriesVisibility.toggled(
            "beta", visible: Set(["alpha"]), allSeries: all
        ) == Set(["alpha", "beta"]))
        #expect(QuotaHistorySeriesVisibility.toggled(
            "alpha", visible: Set(["alpha"]), allSeries: all
        ) == Set(["alpha"]))

        let points = (0..<8).map { index in
            quotaPoint(
                snapshotID: Int64(index), observedAt: TimeInterval(index),
                ratio: Double(index + 1) / 10,
                seriesID: "series-\(index)", seriesLabel: "Provider \(index)"
            )
        }
        let series = QuotaHistoryChartPresentation(points: points).series
        #expect(QuotaHistorySeriesVisibility.visibleIDs(
            mode: .showAll, in: series
        ) == Set(points.map(\.seriesID)))
        #expect(QuotaHistorySeriesVisibility.visibleIDs(
            mode: .focused, in: series
        ).count == 6)
        #expect(QuotaHistorySeriesVisibility.visibleIDs(
            mode: .custom(Set(["series-0", "removed"])), in: series
        ) == Set(["series-0"]))
    }

    @Test("Quota history time labels stay compact while preserving day boundaries")
    func quotaHistoryAxisLabels() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = try #require(TimeZone(identifier: "UTC"))
        let morning = try #require(ActivityTimestamp.date(from: "2026-07-15T08:00:00Z"))
        let midnight = try #require(ActivityTimestamp.date(from: "2026-07-16T00:00:00Z"))

        #expect(QuotaHistoryAxisText.label(
            morning, mode: .hours, calendar: calendar
        ) == "08:00")
        #expect(QuotaHistoryAxisText.label(
            midnight, mode: .hours, calendar: calendar
        ) == "Jul 16\n00:00")
        #expect(QuotaHistoryAxisText.label(
            morning, mode: .dateHours, calendar: calendar
        ) == "Jul 15\n08:00")
        #expect(QuotaHistoryAxisText.label(
            midnight, mode: .days, calendar: calendar
        ) == "Jul 16")
        #expect(QuotaHistoryAxisText.mode(for: 20 * 60 * 60) == .hours)
        #expect(QuotaHistoryAxisText.mode(for: 35 * 60 * 60) == .dateHours)
        #expect(QuotaHistoryAxisText.mode(for: 8 * 24 * 60 * 60) == .days)
    }

    @Test("Quota history time domain is fixed by the complete plot and pads one instant")
    func quotaHistoryTimeDomain() throws {
        let first = try #require(ActivityTimestamp.date(from: "2026-07-15T08:00:00Z"))
        let last = try #require(ActivityTimestamp.date(from: "2026-07-16T08:00:00Z"))
        let points = [
            quotaPoint(snapshotID: 1, observedAt: first.timeIntervalSince1970, ratio: 0.8, seriesLabel: "A"),
            quotaPoint(snapshotID: 2, observedAt: last.timeIntervalSince1970, ratio: 0.6, seriesLabel: "B"),
        ]
        let domain = try #require(QuotaHistoryTimeDomain.make(points: points))
        #expect(domain.lowerBound == first)
        #expect(domain.upperBound == last)
        #expect(domain.span == 24 * 60 * 60)

        let singleton = try #require(QuotaHistoryTimeDomain.make(points: [points[0]]))
        #expect(singleton.span == 120)
        #expect(singleton.lowerBound < first)
        #expect(singleton.upperBound > first)
    }

    @Test("Keyboard bar anchors are centered and reject invalid geometry")
    func keyboardBarAnchors() {
        #expect(KeyboardBarAnchor.centerX(index: 0, count: 3, plotWidth: 300) == 50)
        #expect(KeyboardBarAnchor.centerX(index: 1, count: 3, plotWidth: 300) == 150)
        #expect(KeyboardBarAnchor.centerX(index: 2, count: 3, plotWidth: 300) == 250)
        #expect(KeyboardBarAnchor.centerX(index: -1, count: 3, plotWidth: 300) == nil)
        #expect(KeyboardBarAnchor.centerX(index: 3, count: 3, plotWidth: 300) == nil)
        #expect(KeyboardBarAnchor.centerX(index: 0, count: 0, plotWidth: 300) == nil)
        #expect(KeyboardBarAnchor.centerX(index: 0, count: 3, plotWidth: 0) == nil)
        #expect(KeyboardBarAnchor.centerX(index: 0, count: 3, plotWidth: .nan) == nil)
    }

    @Test("Reduce Motion disables tooltip appearance animation")
    func tooltipMotionPolicy() {
        #expect(TooltipMotionPolicy.appearance(reduceMotion: false) == .opacity)
        #expect(TooltipMotionPolicy.appearance(reduceMotion: true) == .immediate)
    }

    @MainActor
    @Test("Legend toggle clears chart focus and stale IDs converge")
    func legendToggleClearsFocus() throws {
        let preferences = ActivityPreferences(defaults: InMemoryActivityPreferencesStore())
        let store = ActivityViewStore(preferences: preferences)
        store.chartFocus.day = day("2026-07-02")

        #expect(store.toggleModelSeries("m1", availableSeriesIDs: ["m1", "additional-models"]))
        #expect(store.chartFocus.day == nil)
        #expect(store.modelSeriesVisibility.hiddenSeriesIDs == ["m1"])

        store.chartFocus.day = day("2026-07-02")
        #expect(!store.toggleModelSeries("expired", availableSeriesIDs: ["m1", "additional-models"]))
        #expect(store.chartFocus.day == day("2026-07-02"))
        #expect(store.modelSeriesVisibility.hiddenSeriesIDs == ["m1"])

        store.reconcileModelSeries(availableSeriesIDs: ["additional-models"])
        #expect(store.modelSeriesVisibility.hiddenSeriesIDs.isEmpty)

        #expect(store.toggleModelSeries(
            "additional-models", availableSeriesIDs: ["additional-models"]
        ))
        store.chartFocus.day = day("2026-07-02")
        store.showAllModelSeries()
        #expect(store.modelSeriesVisibility.hiddenSeriesIDs.isEmpty)
        #expect(store.chartFocus.day == nil)
    }

    @Test("API spend display rounds only the presentation value")
    func apiSpendDisplay() throws {
        let exact = try #require(Decimal(string: "0.019439868000000001"))
        #expect(APISpendText.display(amount: exact, currency: "USD") == "USD 0.01944")
        #expect(exact == Decimal(string: "0.019439868000000001"))
    }

    @Test("Quota history has eight non-color identities and announces stale summaries")
    func quotaHistoryVisualAccessibility() {
        let patterns = (0..<8).map(QuotaHistoryVisualStyle.dash)
        #expect(Set(patterns).count == 8)
        #expect(QuotaHistoryLegendText.accessibilityValue(
            percentage: "100%", stale: false
        ) == "100% remaining")
        #expect(QuotaHistoryLegendText.accessibilityValue(
            percentage: "100%", stale: true
        ) == "100% remaining, stale")
    }

    @Test("Quota history pointer selects the nearest point in two dimensions and rejects plot exterior")
    func quotaHistoryPointerSelection() {
        let upper = quotaPoint(
            snapshotID: 1, observedAt: 100, ratio: 0.8,
            seriesLabel: "MiniMax · 5-hour"
        )
        let lower = quotaPoint(
            snapshotID: 2, observedAt: 100, ratio: 0.2,
            seriesLabel: "Codex · weekly"
        )
        let targets = [
            QuotaHistoryPlotTarget(point: upper, anchor: ChartAnchor(x: 100, y: 20)),
            QuotaHistoryPlotTarget(point: lower, anchor: ChartAnchor(x: 100, y: 80)),
        ]
        let origin = ChartAnchor(x: 10, y: 10)
        let size = ChartSize(width: 200, height: 100)

        #expect(QuotaHistorySelection.nearest(
            to: ChartAnchor(x: 98, y: 24), plotOrigin: origin,
            plotSize: size, targets: targets
        )?.snapshotID == upper.snapshotID)
        #expect(QuotaHistorySelection.nearest(
            to: ChartAnchor(x: 102, y: 76), plotOrigin: origin,
            plotSize: size, targets: targets
        )?.snapshotID == lower.snapshotID)
        #expect(QuotaHistorySelection.nearest(
            to: ChartAnchor(x: 9.99, y: 50), plotOrigin: origin,
            plotSize: size, targets: targets
        ) == nil)
        #expect(QuotaHistorySelection.nearest(
            to: ChartAnchor(x: 210.01, y: 50), plotOrigin: origin,
            plotSize: size, targets: targets
        ) == nil)
    }

    @Test("Quota history equidistant selection is stable by time label and snapshot")
    func quotaHistoryPointerTieBreak() {
        let pointer = ChartAnchor(x: 100, y: 50)
        let origin = ChartAnchor(x: 0, y: 0)
        let size = ChartSize(width: 200, height: 100)

        let earlier = quotaPoint(
            snapshotID: 30, observedAt: 99, ratio: 0.5,
            seriesLabel: "Zulu · quota"
        )
        let later = quotaPoint(
            snapshotID: 1, observedAt: 100, ratio: 0.5,
            seriesLabel: "Alpha · quota"
        )
        #expect(QuotaHistorySelection.nearest(
            to: pointer, plotOrigin: origin, plotSize: size,
            targets: [
                QuotaHistoryPlotTarget(point: later, anchor: ChartAnchor(x: 110, y: 50)),
                QuotaHistoryPlotTarget(point: earlier, anchor: ChartAnchor(x: 90, y: 50)),
            ]
        )?.snapshotID == earlier.snapshotID)

        let alpha = quotaPoint(
            snapshotID: 20, observedAt: 100, ratio: 0.5,
            seriesLabel: "Alpha · quota"
        )
        let beta = quotaPoint(
            snapshotID: 2, observedAt: 100, ratio: 0.5,
            seriesLabel: "Beta · quota"
        )
        #expect(QuotaHistorySelection.nearest(
            to: pointer, plotOrigin: origin, plotSize: size,
            targets: [
                QuotaHistoryPlotTarget(point: beta, anchor: ChartAnchor(x: 110, y: 50)),
                QuotaHistoryPlotTarget(point: alpha, anchor: ChartAnchor(x: 90, y: 50)),
            ]
        )?.snapshotID == alpha.snapshotID)

        let lowerSnapshot = quotaPoint(
            snapshotID: 3, observedAt: 100, ratio: 0.5,
            seriesLabel: "Alpha · quota"
        )
        #expect(QuotaHistorySelection.nearest(
            to: pointer, plotOrigin: origin, plotSize: size,
            targets: [
                QuotaHistoryPlotTarget(point: alpha, anchor: ChartAnchor(x: 110, y: 50)),
                QuotaHistoryPlotTarget(point: lowerSnapshot, anchor: ChartAnchor(x: 90, y: 50)),
            ]
        )?.snapshotID == lowerSnapshot.snapshotID)
    }

    @Test("Quota history keyboard navigation enters at each edge and stays bounded")
    func quotaHistoryKeyboardNavigation() {
        let points = [
            quotaPoint(snapshotID: 30, observedAt: 300, ratio: 0.3, seriesLabel: "C"),
            quotaPoint(snapshotID: 10, observedAt: 100, ratio: 0.8, seriesLabel: "A"),
            quotaPoint(snapshotID: 20, observedAt: 200, ratio: 0.5, seriesLabel: "B"),
        ]

        #expect(QuotaHistorySelection.move(
            from: nil, direction: .right, points: points
        )?.snapshotID == 10)
        #expect(QuotaHistorySelection.move(
            from: nil, direction: .left, points: points
        )?.snapshotID == 30)
        #expect(QuotaHistorySelection.move(
            from: 20, direction: .left, points: points
        )?.snapshotID == 10)
        #expect(QuotaHistorySelection.move(
            from: 20, direction: .right, points: points
        )?.snapshotID == 30)
        #expect(QuotaHistorySelection.move(
            from: 10, direction: .left, points: points
        )?.snapshotID == 10)
        #expect(QuotaHistorySelection.move(
            from: 30, direction: .right, points: points
        )?.snapshotID == 30)
        #expect(QuotaHistorySelection.move(
            from: nil, direction: .right, points: []
        ) == nil)
    }

    @Test("Quota tooltip is precise, exception-only, and excludes private identifiers")
    func quotaHistoryTooltipText() {
        let secret = "real-account@example.com"
        let stale = quotaPoint(
            snapshotID: 7, observedAt: 100, ratio: 0.553,
            seriesID: "minimax|\(secret)|five-hour|5-hour",
            seriesLabel: "MiniMax · 5-hour · Account 4ac129d2",
            resetsAt: Date(timeIntervalSince1970: 200),
            state: "ok", stale: true
        )
        let text = QuotaHistoryTooltipText.make(point: stale) {
            "T\(Int($0.timeIntervalSince1970))"
        }

        #expect(text.title == "MiniMax · 5-hour · Account 4ac129d2")
        #expect(text.remaining == "55.3% remaining")
        #expect(text.observed == "Observed T100")
        #expect(text.reset == "Resets T200")
        #expect(text.status == "Stale")
        #expect(text.statusSymbol == "clock.badge.exclamationmark")
        #expect(text.accessibilityValue == "MiniMax · 5-hour · Account 4ac129d2, 55.3% remaining, observed T100, resets T200, stale")
        #expect(text.detailAccessibilityValue == "55.3% remaining, observed T100, resets T200, stale")
        let exposed = [
            text.title, text.remaining, text.observed,
            text.reset ?? "", text.status ?? "", text.accessibilityValue,
        ].joined(separator: " ")
        #expect(!exposed.contains(secret))
        #expect(!exposed.contains(stale.seriesID))
        #expect(!exposed.lowercased().contains("payload"))

        let current = quotaPoint(
            snapshotID: 8, observedAt: 300, ratio: 1,
            seriesLabel: "Codex · weekly", resetsAt: nil,
            state: "ok", stale: false
        )
        let currentText = QuotaHistoryTooltipText.make(point: current) {
            "T\(Int($0.timeIntervalSince1970))"
        }
        #expect(currentText.remaining == "100% remaining")
        #expect(currentText.reset == nil)
        #expect(currentText.status == nil)
        #expect(currentText.statusSymbol == nil)
        #expect(!currentText.detailAccessibilityValue.contains("Codex · weekly"))
        #expect(!currentText.accessibilityValue.lowercased().contains("stale"))
        #expect(!currentText.accessibilityValue.lowercased().contains("ok"))

        let unavailable = quotaPoint(
            snapshotID: 9, observedAt: 400, ratio: 0.4,
            seriesLabel: "Kiro · monthly", state: "temporarily_unavailable"
        )
        let unavailableText = QuotaHistoryTooltipText.make(point: unavailable) {
            "T\(Int($0.timeIntervalSince1970))"
        }
        #expect(unavailableText.status == "Temporarily Unavailable")
        #expect(unavailableText.statusSymbol == "exclamationmark.triangle")
        #expect(unavailableText.accessibilityValue.contains("Kiro · monthly"))
    }

    @Test("Swift product categories and metric families stay aligned with the Python registry")
    func capabilityRegistryParity() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let script = #"""
        import json
        from openusage_bar.capabilities import registry
        from openusage_bar.models import Category, canonical_category
        ids = ['minimax','codex','kiro_cli','step_plan','cursor','hermes','openclaw']
        metrics = {}
        for provider_id in ['minimax','codex','kiro_cli','step_plan']:
            metrics[provider_id] = sorted(item.value for item in registry.require(provider_id).metric_families)
        categories = {provider_id: canonical_category(provider_id, Category.SUBSCRIPTION).value for provider_id in ids}
        print(json.dumps({'metrics': metrics, 'categories': categories}, sort_keys=True))
        """#
        let process = Process()
        process.executableURL = root.appendingPathComponent(".build-venv/bin/python")
        process.arguments = ["-c", script]
        process.currentDirectoryURL = root
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = root.path
        process.environment = environment
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        process.waitUntilExit()
        #expect(process.terminationStatus == 0)
        let data = output.fileHandleForReading.readDataToEndOfFile()
        let payload = try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        let metrics = try #require(payload["metrics"] as? [String: [String]])
        let categories = try #require(payload["categories"] as? [String: String])

        for (id, expected) in metrics {
            #expect(ProviderCatalog.descriptor(for: id).metricFamilies.map(\.pythonName).sorted() == expected)
        }
        for (id, expected) in categories {
            #expect(ProviderCatalog.descriptor(for: id).category.pythonName == expected)
        }
    }

    private func quotaPoint(
        snapshotID: Int64, observedAt: TimeInterval, ratio: Double,
        seriesID: String? = nil, seriesLabel: String,
        resetsAt: Date? = nil, state: String = "ok", stale: Bool = false
    ) -> QuotaHistoryPoint {
        QuotaHistoryPoint(
            snapshotID: snapshotID,
            observedAt: Date(timeIntervalSince1970: observedAt),
            day: day("2026-07-15"), remainingRatio: ratio,
            seriesID: seriesID ?? "series-\(snapshotID)",
            lineSegmentID: "window-\(snapshotID)",
            styleKey: "style-\(snapshotID)", seriesLabel: seriesLabel,
            resetsAt: resetsAt, state: state, stale: stale
        )
    }
}

private extension ProviderMetricFamily {
    var pythonName: String {
        switch self {
        case .subscriptionQuota: "subscription_quota"
        case .tokenActivity: "token_activity"
        case .billing: "billing"
        case .operational: "operational"
        }
    }
}

private extension ProviderProductCategory {
    var pythonName: String {
        switch self { case .subscription: "subscription"; case .api: "api"; case .localTool: "local" }
    }
}
