import Foundation
import Testing
@testable import UsageCore

@Suite("Usage Details pure presentation")
struct UsageDetailsTests {
    private func day(_ value: String) -> LocalDay { try! LocalDay(value) }

    private func record(
        _ dayValue: String,
        provider: String = "codex",
        model: String,
        tokens: Int64,
        quality: String = "exact",
        sourceID: String = "legacy"
    ) -> DailyUsage {
        DailyUsage(
            day: day(dayValue), providerID: provider, accountRef: "", modelID: model,
            inputTokens: tokens, outputTokens: 0, cacheReadTokens: 0,
            cacheCreationTokens: 0, reasoningTokens: nil, totalTokens: tokens,
            costAmount: nil, costCurrency: nil, costBasis: nil, quality: quality,
            importedAt: "2026-07-14T09:00:00Z", revision: 1,
            recordID: "\(dayValue).\(provider).\(model)", sourceID: sourceID
        )
    }

    private func coverage(
        _ value: String, provider: String = "codex", covered: Bool = true
    ) -> CoverageDay {
        CoverageDay(day: day(value), providerID: provider, accountRef: "", isCovered: covered)
    }

    @Test("Thirty day stacked series uses stable day model IDs and leaves missing days as gaps")
    func stackedSeriesAndMissingGap() {
        let records = (1...7).map { index in
            record("2026-07-02", model: "m\(index)", tokens: Int64(8 - index) * 1_000_000)
        } + [record("2026-07-04", model: "m1", tokens: 2_000_000, quality: "derived")]
        let dataset = ActivityDataset(
            records: records,
            coverage: [coverage("2026-07-02"), coverage("2026-07-03", covered: false), coverage("2026-07-04")],
            knownScopes: [ProviderScope(providerID: "codex", accountRef: "")],
            revision: 7
        )

        let model = UsageDetailsAggregator.make(
            from: dataset,
            metricRange: day("2026-07-02")...day("2026-07-04")
        )

        #expect(model.seriesPoints.allSatisfy { $0.id == "\($0.day.rawValue)|\($0.modelID)" })
        #expect(Set(model.seriesPoints.map(\.modelID)) == Set((1...7).map { "m\($0)" }))
        #expect(!model.seriesPoints.contains { $0.day == day("2026-07-03") })
        #expect(model.chartDays.count == 30)
        #expect(model.chartDays.first?.day == day("2026-06-05"))
        #expect(model.chartDays.last?.day == day("2026-07-04"))
        #expect(model.chartDays.first { $0.day == day("2026-07-02") }?.state == .coveredActive)
        #expect(model.chartDays.first { $0.day == day("2026-07-03") }?.state == .missing)
        #expect(model.chartDays.first { $0.day == day("2026-07-03") }?.totalTokens == nil)
        #expect(model.chartDays.first { $0.day == day("2026-07-03") }?.hasObservedBreakdown == false)
        #expect(model.chartDays.first { $0.day == day("2026-07-03") }?.accessibilitySummary.contains("Input 0") == false)
        #expect(model.chartDays.first { $0.day == day("2026-07-04") }?.quality == .estimated)
        #expect(model.seriesPoints.filter { $0.day == day("2026-07-02") }.reduce(0) { $0 + $1.tokens } == 28_000_000)
    }

    @Test("Model chart stays at thirty days while metrics and heatmap keep their own ranges")
    func independentPresentationRanges() {
        let records = [
            record("2025-07-15", model: "old", tokens: 1),
            record("2026-06-14", model: "before-chart", tokens: 5),
            record("2026-06-15", model: "month-edge", tokens: 2),
            record("2026-07-08", model: "week-edge", tokens: 3),
            record("2026-07-14", model: "today", tokens: 4),
            record("2026-07-15", model: "future", tokens: 100),
        ]
        let dataset = ActivityDataset(
            records: records,
            coverage: records.map { coverage($0.day.rawValue) },
            knownScopes: [ProviderScope(providerID: "codex", accountRef: "")], revision: 1
        )
        let end = day("2026-07-14")
        let expectedMetricTokens: [UsagePeriod: Int64] = [
            .day: 4,
            .week: 7,
            .month: 9,
            .year: 15,
        ]
        let expectedMetricDays: [UsagePeriod: Int] = [
            .day: 1,
            .week: 2,
            .month: 3,
            .year: 5,
        ]
        for period in UsagePeriod.allCases {
            let range = period.range(ending: end)
            let heatmapRange = UsagePeriod.year.range(ending: end)
            let model = UsageDetailsAggregator.make(
                from: dataset, heatmapRange: heatmapRange, metricRange: range
            )

            #expect(model.chartDays.count == 30)
            #expect(model.chartDays.first?.day == day("2026-06-15"))
            #expect(model.chartDays.last?.day == end)
            #expect(model.seriesPoints.allSatisfy {
                (day("2026-06-15")...end).contains($0.day)
            })
            #expect(Set(model.seriesPoints.map(\.modelID)) == Set(["month-edge", "week-edge", "today"]))
            #expect(model.metrics.observedTokens == expectedMetricTokens[period])
            #expect(model.metrics.activeDays == expectedMetricDays[period])
            #expect(model.heatmapDays.count == 365)
            #expect(model.heatmapDays.first?.day == heatmapRange.lowerBound)
            #expect(model.heatmapDays.last?.day == heatmapRange.upperBound)
        }
    }

    @Test("Provider and model filters keep ordinary model series explicit")
    func chartFiltersKeepExplicitModels() {
        let records = [
            record("2026-07-14", model: "m1", tokens: 70),
            record("2026-07-14", model: "m2", tokens: 60),
            record("2026-07-14", model: "m3", tokens: 50),
            record("2026-07-14", model: "m4", tokens: 40),
            record("2026-07-14", model: "m5", tokens: 30),
            record("2026-07-14", model: "m6", tokens: 20),
            record("2026-07-14", provider: "minimax", model: "minimax-m2", tokens: 1_000),
        ]
        let dataset = ActivityDataset(
            records: records,
            coverage: [
                coverage("2026-07-14"),
                coverage("2026-07-14", provider: "minimax"),
            ],
            knownScopes: [
                ProviderScope(providerID: "codex", accountRef: ""),
                ProviderScope(providerID: "minimax", accountRef: ""),
            ],
            revision: 1
        )
        let end = day("2026-07-14")
        let codex = UsageDetailsAggregator.make(
            from: dataset, metricRange: UsagePeriod.day.range(ending: end),
            providerIDs: ["codex"]
        )
        let codexEnd = codex.chartDays.last { $0.day == end }

        #expect(codex.metrics.observedTokens == 270)
        #expect(codexEnd?.composition.map(\.modelID) == ["m1", "m2", "m3", "m4", "m5", "m6"])
        #expect(codexEnd?.composition.last?.tokens == 20)
        #expect(!codex.seriesPoints.contains { $0.modelID == "minimax-m2" })

        let selectedModels = UsageDetailsAggregator.make(
            from: dataset, metricRange: UsagePeriod.day.range(ending: end),
            providerIDs: ["codex"], modelIDs: ["m1", "m6"]
        )
        #expect(selectedModels.metrics.observedTokens == 90)
        #expect(Set(selectedModels.seriesPoints.map(\.modelID)) == Set(["m1", "m6"]))
    }

    @Test("Model series visibility hides and restores immutable presentation values")
    func modelSeriesVisibility() throws {
        let records = (1...7).map { index in
            record("2026-07-02", model: "m\(index)", tokens: Int64(8 - index) * 1_000_000)
        }
        let model = UsageDetailsAggregator.make(
            from: ActivityDataset(
                records: records,
                coverage: [coverage("2026-07-02")],
                knownScopes: [ProviderScope(providerID: "codex", accountRef: "")],
                revision: 7
            ),
            metricRange: day("2026-07-02")...day("2026-07-02")
        )
        let original = try #require(model.chartDays.first { $0.day == day("2026-07-02") })
        var visibility = ModelSeriesVisibility(hiddenSeriesIDs: ["expired-model"])
        visibility.reconcile(availableSeriesIDs: model.modelSeriesIDs)

        #expect(visibility.hiddenSeriesIDs.isEmpty)
        let hidM1 = visibility.toggle("m1", availableSeriesIDs: model.modelSeriesIDs)
        #expect(hidM1)
        let hidden = model.modelSeriesPresentation(visibility: visibility)
        let hiddenDay = try #require(hidden.chartDays.first { $0.day == original.day })
        #expect(!hidden.seriesPoints.contains { $0.modelID == "m1" })
        #expect(hiddenDay.composition.map(\.modelID) == ["m2", "m3", "m4", "m5", "m6", "m7"])
        #expect(hiddenDay.totalTokens == original.totalTokens)
        #expect(model.chartDays.first { $0.day == original.day } == original)

        let restoredM1 = visibility.toggle("m1", availableSeriesIDs: model.modelSeriesIDs)
        #expect(restoredM1)
        let restored = model.modelSeriesPresentation(visibility: visibility)
        #expect(restored.seriesPoints == model.seriesPoints)
        #expect(restored.chartDays == model.chartDays)
    }

    @Test("Overflow hides independently and unknown is provider-attributed")
    func overflowAndUnattributedSeriesVisibility() throws {
        let records = (1...14).map { index in
            record("2026-07-02", model: "m\(index)", tokens: Int64(15 - index))
        } + [record("2026-07-02", model: "unknown", tokens: 100)]
        let model = UsageDetailsAggregator.make(
            from: ActivityDataset(
                records: records,
                coverage: [coverage("2026-07-02")],
                knownScopes: [ProviderScope(providerID: "codex", accountRef: "")],
                revision: 1
            ),
            metricRange: day("2026-07-02")...day("2026-07-02")
        )
        var visibility = ModelSeriesVisibility()

        #expect(model.modelSeriesIDs.contains("unattributed:codex"))
        #expect(DisplayText.model("unattributed:codex") == "Codex · Unattributed")
        #expect(!model.modelSeriesIDs.contains("unknown"))
        let hidOverflow = visibility.toggle(
            "additional-models", availableSeriesIDs: model.modelSeriesIDs
        )
        #expect(hidOverflow)
        let presentation = model.modelSeriesPresentation(visibility: visibility)
        let selected = try #require(presentation.chartDays.first { $0.day == day("2026-07-02") })
        #expect(!presentation.seriesPoints.contains { $0.modelID == "additional-models" })
        #expect(!selected.composition.contains { $0.modelID == "additional-models" })
        #expect(selected.totalTokens == 205)
        #expect(presentation.series.first { $0.modelID == "m1" }?.styleIndex
            == model.modelSeriesPresentation().series.first { $0.modelID == "m1" }?.styleIndex)
    }

    @Test("Absent model data is distinct from a user hiding every series")
    func absentVersusHiddenModelSeries() {
        let zeroModel = UsageDetailsAggregator.make(
            from: ActivityDataset(
                records: [],
                coverage: [coverage("2026-07-02")],
                knownScopes: [ProviderScope(providerID: "codex", accountRef: "")],
                revision: 1
            ),
            metricRange: day("2026-07-02")...day("2026-07-02")
        )
        #expect(zeroModel.modelSeriesPresentation().displayState == .noSeries)

        let activeModel = UsageDetailsAggregator.make(
            from: ActivityDataset(
                records: [record("2026-07-02", model: "m1", tokens: 10)],
                coverage: [coverage("2026-07-02")],
                knownScopes: [ProviderScope(providerID: "codex", accountRef: "")],
                revision: 1
            ),
            metricRange: day("2026-07-02")...day("2026-07-02")
        )
        var visibility = ModelSeriesVisibility(hiddenSeriesIDs: ["m1"])
        visibility.reconcile(availableSeriesIDs: activeModel.modelSeriesIDs)
        #expect(activeModel.modelSeriesPresentation(visibility: visibility).displayState == .allHidden)
        #expect(activeModel.modelSeriesPresentation().displayState == .visible)
    }

    @Test("July 2 composition lives in one hover and keyboard payload")
    func focusPayload() throws {
        let dataset = ActivityDataset(
            records: [
                record("2026-07-02", model: "gpt-5.5", tokens: 38_400_000),
                record("2026-07-02", model: "minimax-m2.5", tokens: 18_700_000),
                record("2026-07-02", model: "cursor-auto", tokens: 9_100_000),
                record("2026-07-02", model: "kiro", tokens: 5_000_000),
                record("2026-07-02", model: "other-model", tokens: 3_000_000),
            ],
            coverage: [coverage("2026-07-02")],
            knownScopes: [ProviderScope(providerID: "codex", accountRef: "")],
            revision: 1
        )
        let model = UsageDetailsAggregator.make(
            from: dataset,
            metricRange: day("2026-07-02")...day("2026-07-02")
        )
        let payload = try #require(model.chartDays.first { $0.day == day("2026-07-02") })

        #expect(payload.day == day("2026-07-02"))
        #expect(payload.totalTokens == 74_200_000)
        #expect(payload.composition.reduce(0) { $0 + $1.tokens } == 74_200_000)
        #expect(payload.quality == .exact)
        #expect(payload.lastCollectionAt == "2026-07-14T09:00:00Z")
        #expect(payload.accessibilitySummary.contains("74.2M Tokens"))
        #expect(payload.accessibilitySummary.contains("GPT-5.5"))
        #expect(payload.accessibilitySummary.contains("2026-07-14T09:00:00Z"))
    }

    @Test("Daily chart carries a complete non-additive Token breakdown")
    func dailyChartTokenBreakdown() throws {
        let value = DailyUsage(
            day: day("2026-07-02"), providerID: "codex", accountRef: "",
            modelID: "gpt-5.6-sol", inputTokens: 100, outputTokens: 20,
            cacheReadTokens: 80, cacheCreationTokens: 4, reasoningTokens: 3,
            totalTokens: 120, costAmount: nil, costCurrency: nil, costBasis: nil,
            quality: "exact", importedAt: "2026-07-14T09:00:00Z", revision: 1,
            recordID: "breakdown"
        )
        let model = UsageDetailsAggregator.make(
            from: ActivityDataset(
                records: [value], coverage: [coverage("2026-07-02")],
                knownScopes: [ProviderScope(providerID: "codex", accountRef: "")], revision: 1
            ),
            metricRange: day("2026-07-02")...day("2026-07-02")
        )
        let payload = try #require(model.chartDays.last)

        #expect(payload.observedBreakdown.totalTokens == 120)
        #expect(payload.observedBreakdown.inputTokens == 100)
        #expect(payload.observedBreakdown.outputTokens == 20)
        #expect(payload.observedBreakdown.cacheReadTokens == 80)
        #expect(payload.observedBreakdown.cacheCreationTokens == 4)
        #expect(payload.accessibilitySummary.contains("Input 100"))
        #expect(payload.accessibilitySummary.contains("Output 20"))
        #expect(payload.accessibilitySummary.contains("Cache Read 80"))
        #expect(payload.accessibilitySummary.contains("Cache Write 4"))
    }

    @Test("Chart day exposes selected source quality and collection time")
    func chartDayProvenance() throws {
        let dataset = ActivityDataset(
            records: [record(
                "2026-07-14", model: "gpt-5.6-sol", tokens: 300,
                quality: "fallback", sourceID: "openusage.daily"
            )],
            coverage: [CoverageDay(
                day: day("2026-07-14"), providerID: "codex", accountRef: "",
                isCovered: true, sourceID: "openusage.daily"
            )],
            knownScopes: [ProviderScope(providerID: "codex", accountRef: "")],
            revision: 2
        )

        let model = UsageDetailsAggregator.make(
            from: dataset,
            metricRange: day("2026-07-14")...day("2026-07-14")
        )
        let value = try #require(model.chartDays.last)

        #expect(value.sourceIDs == ["openusage.daily"])
        #expect(value.qualityIDs == ["fallback"])
        #expect(value.lastCollectionAt == "2026-07-14T09:00:00Z")
        #expect(value.accessibilitySummary.contains("openusage.daily"))
        #expect(value.accessibilitySummary.contains("fallback"))
    }

    @Test("Period ranges are calendar bounded and never exceed repository limits")
    func periodRanges() throws {
        let end = day("2026-07-14")
        #expect(UsagePeriod.day.range(ending: end) == day("2026-07-14")...end)
        #expect(UsagePeriod.week.range(ending: end) == day("2026-07-08")...end)
        #expect(UsagePeriod.month.range(ending: end) == day("2026-06-15")...end)
        #expect(UsagePeriod.year.range(ending: end) == day("2025-07-15")...end)
        #expect(UsagePeriod.year.repositoryRange(ending: end).dayCount == 365)
    }

    @Test("Heatmap keyboard movement follows seven fixed rows and clamps")
    func heatmapNavigation() {
        #expect(HeatmapNavigation.destination(from: 8, direction: .up, count: 30) == 7)
        #expect(HeatmapNavigation.destination(from: 8, direction: .down, count: 30) == 9)
        #expect(HeatmapNavigation.destination(from: 8, direction: .left, count: 30) == 1)
        #expect(HeatmapNavigation.destination(from: 8, direction: .right, count: 30) == 15)
        #expect(HeatmapNavigation.destination(from: 1, direction: .left, count: 30) == 1)
        #expect(HeatmapNavigation.destination(from: 29, direction: .right, count: 30) == 29)
    }

    @Test("Annual calendar maps Tuesday into row two and remains 53 columns")
    func calendarLayout() throws {
        let range = day("2025-07-15")...day("2026-07-14")
        let details = range.days.map { value in
            HeatmapDayDetail(
                activity: ActivityDay(day: value, state: .coveredZero, totalTokens: 0, observedTokens: 0, heatLevel: 0),
                quality: .exact, lastCollectionAt: nil, isStale: false
            )
        }
        let layout = HeatmapCalendarLayout(range: range, details: details)

        #expect(layout.columnCount == 53)
        #expect(layout.slots.count == 371)
        #expect(layout.slots.filter { $0.detail != nil }.count == 365)
        #expect(layout.slots[0].detail == nil)
        #expect(layout.slots[1].detail == nil)
        #expect(layout.slots[2].detail?.activity.day == day("2025-07-15"))
        #expect(layout.slots[2].row == 2)
        #expect(layout.slots[2].column == 0)
        #expect(layout.slots[366].detail?.activity.day == day("2026-07-14"))
        #expect(layout.slots[366].row == 2)
        #expect(layout.slots[366].column == 52)
        #expect(layout.slots[367...370].allSatisfy { $0.detail == nil })
    }

    @Test("Month anchors use actual week columns at the fixed cell pitch")
    func calendarMonthAnchors() {
        let range = day("2025-07-15")...day("2026-07-14")
        let layout = HeatmapCalendarLayout(range: range, details: [])
        #expect(layout.monthAnchors.map(\.column) == [0, 2, 7, 11, 15, 20, 24, 29, 33, 37, 41, 46, 50])
        #expect(layout.monthAnchors.map { HeatmapCalendarLayout.x(forColumn: $0.column, pitch: 17) }
            == [0, 34, 119, 187, 255, 340, 408, 493, 561, 629, 697, 782, 850])
    }

    @Test("Grouped heatmap navigation never selects a calendar placeholder")
    func calendarNavigation() throws {
        let range = day("2025-07-15")...day("2026-07-14")
        let details = range.days.map { value in
            HeatmapDayDetail(
                activity: ActivityDay(day: value, state: .coveredZero, totalTokens: 0, observedTokens: 0, heatLevel: 0),
                quality: .exact, lastCollectionAt: nil, isStale: false
            )
        }
        let layout = HeatmapCalendarLayout(range: range, details: details)
        #expect(layout.destination(from: 2, direction: .up) == 2)
        #expect(layout.destination(from: 2, direction: .left) == 2)
        #expect(layout.destination(from: 2, direction: .down) == 3)
        #expect(layout.destination(from: 2, direction: .right) == 9)
        #expect(layout.slots[layout.destination(from: 366, direction: .right)].detail != nil)
        #expect(layout.accessibilityDayCount == 365)
    }

    @Test("Stale heatmap detail keeps intensity and exposes effective quality")
    func staleHeatmapDetail() {
        let activity = ActivityDay(
            day: day("2026-07-02"), state: .coveredActive,
            totalTokens: 9, observedTokens: 9, heatLevel: 4
        )
        let current = HeatmapDayDetail(
            activity: activity, quality: .exact,
            lastCollectionAt: "2026-07-02T23:59:00Z", isStale: false
        )
        let stale = HeatmapDayDetail(
            activity: activity, quality: .exact,
            lastCollectionAt: "2026-07-02T23:59:00Z", isStale: true
        )
        #expect(current.activity.heatLevel == stale.activity.heatLevel)
        #expect(!current.accessibilitySummary.contains("Stale"))
        #expect(stale.accessibilitySummary.contains("Stale"))
        #expect(stale.accessibilitySummary.contains("2026-07-02T23:59:00Z"))
    }

    @Test("Quota history dates honor injected local calendar and series never cross quota windows")
    func quotaHistoryPresentation() throws {
        var calendar = Calendar(identifier: .gregorian)
        let singapore = try #require(TimeZone(identifier: "Asia/Singapore"))
        calendar.timeZone = singapore
        let items = [
            QuotaHistoryItem(
                snapshotID: 1, recordID: "plan", observedAt: "2026-07-13T16:30:00Z",
                providerID: "minimax", accountRef: "primary", quotaName: "5-hour",
                remainingRatio: 0.8, state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 2, recordID: "plan", observedAt: "2026-07-14T15:59:59Z",
                providerID: "minimax", accountRef: "primary", quotaName: "weekly",
                remainingRatio: 0.7, state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 3, recordID: "plan", observedAt: "2026-07-14T16:00:00Z",
                providerID: "minimax", accountRef: "secondary", quotaName: "weekly",
                remainingRatio: 0.6, state: "ok", stale: false
            ),
        ]

        let points = try QuotaHistoryPresentation.make(items: items, calendar: calendar)
        #expect(points.map(\.day.rawValue) == ["2026-07-14", "2026-07-14", "2026-07-15"])
        #expect(Set(points.map(\.seriesID)).count == 3)
        #expect(Set(points.map(\.styleKey)).count == 3)
        #expect(points.allSatisfy { !$0.styleKey.contains("primary") && !$0.styleKey.contains("secondary") })
        #expect(points[0].seriesID.contains("primary|plan|5-hour"))
        #expect(points[1].seriesLabel.contains("weekly"))
        #expect(points[2].seriesLabel.contains("Account "))
        #expect(!points[2].seriesLabel.contains("secondary"))
    }

    @Test("Quota history keeps color identity while reset windows form separate line segments")
    func quotaHistoryResetWindowSegments() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = try #require(TimeZone(identifier: "UTC"))
        let items = [
            QuotaHistoryItem(
                snapshotID: 30, recordID: "plan", observedAt: "2026-07-15T04:01:00Z",
                providerID: "minimax", accountRef: "private-account", quotaName: "5-hour",
                remainingRatio: 0.99, resetsAt: "2026-07-15T09:00:00Z",
                state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 10, recordID: "plan", observedAt: "2026-07-15T03:59:00Z",
                providerID: "minimax", accountRef: "private-account", quotaName: "5-hour",
                remainingRatio: 0.24, resetsAt: "2026-07-15T04:00:00Z",
                state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 20, recordID: "plan", observedAt: "2026-07-15T03:00:00Z",
                providerID: "minimax", accountRef: "private-account", quotaName: "5-hour",
                remainingRatio: 0.48, resetsAt: "2026-07-15T04:00:00Z",
                state: "ok", stale: false
            ),
        ]

        let points = try QuotaHistoryPresentation.make(items: items, calendar: calendar)

        #expect(points.map(\.snapshotID) == [20, 10, 30])
        #expect(Set(points.map(\.seriesID)).count == 1)
        #expect(Set(points.map(\.styleKey)).count == 1)
        #expect(points[0].lineSegmentID == points[1].lineSegmentID)
        #expect(points[1].lineSegmentID != points[2].lineSegmentID)
        #expect(Set(points.map(\.lineSegmentID)).count == 2)
        #expect(points.allSatisfy {
            !$0.styleKey.contains("private-account") && !$0.seriesLabel.contains("private-account")
        })
    }

    @Test("Quota history treats a material replenishment as a new visual window")
    func quotaHistoryInferredReplenishmentSegments() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = try #require(TimeZone(identifier: "UTC"))
        let items = [
            QuotaHistoryItem(
                snapshotID: 1, recordID: "plan", observedAt: "2026-07-15T01:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.8, resetsAt: nil, state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 2, recordID: "plan", observedAt: "2026-07-15T02:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.45, resetsAt: nil, state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 3, recordID: "plan", observedAt: "2026-07-15T03:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.95, resetsAt: nil, state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 4, recordID: "plan", observedAt: "2026-07-15T04:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.7, resetsAt: nil, state: "ok", stale: false
            ),
        ]

        let points = try QuotaHistoryPresentation.make(items: items, calendar: calendar)

        #expect(points[0].lineSegmentID == points[1].lineSegmentID)
        #expect(points[1].lineSegmentID != points[2].lineSegmentID)
        #expect(points[2].lineSegmentID == points[3].lineSegmentID)
        let chart = QuotaHistoryChartPresentation(points: points)
        #expect(chart.resetMarkers.map(\.snapshotID) == [3])
        #expect(chart.latestMarkers.map(\.snapshotID) == [4])
    }

    @Test("Quota history uses a quantized five percentage point replenishment boundary")
    func quotaHistoryReplenishmentBoundary() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = try #require(TimeZone(identifier: "UTC"))
        let items = [
            QuotaHistoryItem(
                snapshotID: 1, recordID: "plan", observedAt: "2026-07-15T01:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.5, state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 2, recordID: "plan", observedAt: "2026-07-15T02:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.549, state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 3, recordID: "plan", observedAt: "2026-07-15T03:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.599, state: "ok", stale: false
            ),
        ]

        let points = try QuotaHistoryPresentation.make(items: items, calendar: calendar)

        #expect(points[0].lineSegmentID == points[1].lineSegmentID)
        #expect(points[1].lineSegmentID != points[2].lineSegmentID)
    }

    @Test("Quota history tolerates reset timestamp drift and display-invisible ratio noise")
    func quotaHistoryResetDriftAndRatioNoise() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = try #require(TimeZone(identifier: "UTC"))
        let items = [
            QuotaHistoryItem(
                snapshotID: 1, recordID: "weekly", observedAt: "2026-07-15T01:00:00Z",
                providerID: "codex", accountRef: "", quotaName: "weekly",
                remainingRatio: 1, resetsAt: "2026-07-20T00:00:00Z",
                state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 2, recordID: "weekly", observedAt: "2026-07-15T02:00:00Z",
                providerID: "codex", accountRef: "", quotaName: "weekly",
                remainingRatio: 0.99996, resetsAt: "2026-07-20T00:00:30Z",
                state: "ok", stale: false
            ),
        ]

        let points = try QuotaHistoryPresentation.make(items: items, calendar: calendar)
        let chart = QuotaHistoryChartPresentation(points: points)

        #expect(points[0].lineSegmentID == points[1].lineSegmentID)
        #expect(chart.changingSeries.isEmpty)
        #expect(chart.unchangedSeries.map(\.seriesID) == [points[0].seriesID])
    }

    @Test("Quota history carries reset identity through missing metadata")
    func quotaHistoryMissingResetMetadata() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = try #require(TimeZone(identifier: "UTC"))
        let items = [
            QuotaHistoryItem(
                snapshotID: 1, recordID: "plan", observedAt: "2026-07-15T01:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.8, resetsAt: "2026-07-15T05:00:00Z",
                state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 2, recordID: "plan", observedAt: "2026-07-15T02:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.7, resetsAt: nil, state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 3, recordID: "plan", observedAt: "2026-07-15T03:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.6, resetsAt: "2026-07-15T05:00:30Z",
                state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 4, recordID: "plan", observedAt: "2026-07-15T04:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.5, resetsAt: "2026-07-15T10:00:00Z",
                state: "ok", stale: false
            ),
        ]

        let points = try QuotaHistoryPresentation.make(items: items, calendar: calendar)

        #expect(Set(points.prefix(3).map(\.lineSegmentID)).count == 1)
        #expect(points[2].lineSegmentID != points[3].lineSegmentID)
    }

    @Test("Quota history learns a reset identity after inferred replenishment without splitting twice")
    func quotaHistoryLearnsResetAfterReplenishment() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = try #require(TimeZone(identifier: "UTC"))
        let items = [
            QuotaHistoryItem(
                snapshotID: 1, recordID: "plan", observedAt: "2026-07-15T01:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.4, resetsAt: "2026-07-15T05:00:00Z",
                state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 2, recordID: "plan", observedAt: "2026-07-15T02:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.95, resetsAt: nil, state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 3, recordID: "plan", observedAt: "2026-07-15T03:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.9, resetsAt: "2026-07-15T10:00:00Z",
                state: "ok", stale: false
            ),
        ]

        let points = try QuotaHistoryPresentation.make(items: items, calendar: calendar)

        #expect(points[0].lineSegmentID != points[1].lineSegmentID)
        #expect(points[1].lineSegmentID == points[2].lineSegmentID)
    }

    @Test("Quota history breaks an exceptional sampling gap without calling it a reset")
    func quotaHistorySamplingGap() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = try #require(TimeZone(identifier: "UTC"))
        let items = [
            (1, "2026-07-15T00:00:00Z", 0.90),
            (2, "2026-07-15T00:15:00Z", 0.86),
            (3, "2026-07-15T00:30:00Z", 0.82),
            (4, "2026-07-15T04:00:00Z", 0.60),
            (5, "2026-07-15T04:15:00Z", 0.56),
        ].map { snapshotID, observedAt, ratio in
            QuotaHistoryItem(
                snapshotID: Int64(snapshotID), recordID: "plan", observedAt: observedAt,
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: ratio, resetsAt: "2026-07-15T05:00:00Z",
                state: "ok", stale: false
            )
        }

        let points = try QuotaHistoryPresentation.make(items: items, calendar: calendar)
        let chart = QuotaHistoryChartPresentation(points: points)

        #expect(Set(points.prefix(3).map(\.lineSegmentID)).count == 1)
        #expect(points[2].lineSegmentID != points[3].lineSegmentID)
        #expect(points[3].lineSegmentID == points[4].lineSegmentID)
        #expect(points[3].segmentStart == .samplingGap)
        #expect(chart.resetMarkers.isEmpty)
    }

    @Test("Quota history keeps a regular sparse cadence connected")
    func quotaHistoryRegularSparseCadence() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = try #require(TimeZone(identifier: "UTC"))
        let items = (0..<5).map { index in
            QuotaHistoryItem(
                snapshotID: Int64(index), recordID: "monthly",
                observedAt: "2026-07-\(String(format: "%02d", 10 + index))T00:00:00Z",
                providerID: "kiro_cli", accountRef: "", quotaName: "monthly",
                remainingRatio: 0.9 - Double(index) * 0.05,
                resetsAt: "2026-08-01T00:00:00Z", state: "ok", stale: false
            )
        }

        let points = try QuotaHistoryPresentation.make(items: items, calendar: calendar)

        #expect(Set(points.map(\.lineSegmentID)).count == 1)
        #expect(points.dropFirst().allSatisfy { $0.segmentStart == .continuation })
    }

    @Test("Quota history learns a stable new cadence after one sampling gap")
    func quotaHistoryCadenceChange() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = try #require(TimeZone(identifier: "UTC"))
        let minutes = [0, 15, 30, 45, 105, 165, 225, 285]
        let items = minutes.enumerated().map { index, minute in
            QuotaHistoryItem(
                snapshotID: Int64(index + 1), recordID: "plan",
                observedAt: String(format: "2026-07-15T%02d:%02d:00Z", minute / 60, minute % 60),
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.95 - Double(index) * 0.04,
                resetsAt: "2026-07-16T00:00:00Z", state: "ok", stale: false
            )
        }

        let prefix = try QuotaHistoryPresentation.make(
            items: Array(items.prefix(6)), calendar: calendar
        )
        let full = try QuotaHistoryPresentation.make(items: items, calendar: calendar)

        #expect(Set(full.prefix(4).map(\.lineSegmentID)).count == 1)
        #expect(full[3].lineSegmentID != full[4].lineSegmentID)
        #expect(Set(full.suffix(4).map(\.lineSegmentID)).count == 1)
        #expect(full[4].segmentStart == .samplingGap)
        #expect(prefix.map(\.lineSegmentID) == Array(full.prefix(prefix.count).map(\.lineSegmentID)))
    }

    @Test("Quota history can break a gap after one learned interval")
    func quotaHistoryThreePointGap() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = try #require(TimeZone(identifier: "UTC"))
        let items = [
            (1, "2026-07-15T00:00:00Z", 0.90),
            (2, "2026-07-15T00:15:00Z", 0.85),
            (3, "2026-07-15T04:00:00Z", 0.60),
        ].map { snapshotID, observedAt, ratio in
            QuotaHistoryItem(
                snapshotID: Int64(snapshotID), recordID: "plan", observedAt: observedAt,
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: ratio, resetsAt: "2026-07-16T00:00:00Z",
                state: "ok", stale: false
            )
        }

        let points = try QuotaHistoryPresentation.make(items: items, calendar: calendar)

        #expect(points[0].lineSegmentID == points[1].lineSegmentID)
        #expect(points[1].lineSegmentID != points[2].lineSegmentID)
        #expect(points[2].segmentStart == .samplingGap)
    }

    @Test("Quota chart retains visible markers for singleton windows")
    func quotaHistorySingletonWindowMarkers() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = try #require(TimeZone(identifier: "UTC"))
        let items = [
            QuotaHistoryItem(
                snapshotID: 1, recordID: "plan", observedAt: "2026-07-15T01:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.4, state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 2, recordID: "plan", observedAt: "2026-07-15T02:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.95, state: "ok", stale: false
            ),
        ]

        let points = try QuotaHistoryPresentation.make(items: items, calendar: calendar)
        let chart = QuotaHistoryChartPresentation(points: points)

        #expect(chart.isolatedSegmentMarkers.map(\.snapshotID) == [1, 2])
        #expect(chart.resetMarkers.map(\.snapshotID) == [2])
        #expect(chart.latestMarkers.map(\.snapshotID) == [2])
    }

    @Test("Quota chart separates changing and unchanged series without dropping observations")
    func quotaHistoryChartPresentation() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = try #require(TimeZone(identifier: "UTC"))
        let items = [
            QuotaHistoryItem(
                snapshotID: 3, recordID: "five-hour", observedAt: "2026-07-15T03:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.48, resetsAt: "2026-07-15T05:00:00Z",
                state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 7, recordID: "five-hour", observedAt: "2026-07-15T04:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.99, resetsAt: "2026-07-15T10:00:00Z",
                state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 1, recordID: "five-hour", observedAt: "2026-07-15T01:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.82, resetsAt: "2026-07-15T05:00:00Z",
                state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 2, recordID: "five-hour", observedAt: "2026-07-15T02:00:00Z",
                providerID: "minimax", accountRef: "", quotaName: "5-hour",
                remainingRatio: 0.65, resetsAt: "2026-07-15T05:00:00Z",
                state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 6, recordID: "weekly", observedAt: "2026-07-15T03:00:00Z",
                providerID: "codex", accountRef: "", quotaName: "weekly",
                remainingRatio: 1, resetsAt: "2026-07-20T00:00:00Z",
                state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 4, recordID: "weekly", observedAt: "2026-07-15T01:00:00Z",
                providerID: "codex", accountRef: "", quotaName: "weekly",
                remainingRatio: 1, resetsAt: "2026-07-20T00:00:00Z",
                state: "ok", stale: false
            ),
            QuotaHistoryItem(
                snapshotID: 5, recordID: "weekly", observedAt: "2026-07-15T02:00:00Z",
                providerID: "codex", accountRef: "", quotaName: "weekly",
                remainingRatio: 1, resetsAt: "2026-07-20T00:00:00Z",
                state: "ok", stale: false
            ),
        ]
        let points = try QuotaHistoryPresentation.make(items: items, calendar: calendar)

        let chart = QuotaHistoryChartPresentation(points: points)

        #expect(chart.series.count == 2)
        #expect(chart.series.flatMap(\.points).map(\.snapshotID).sorted() == [1, 2, 3, 4, 5, 6, 7])
        #expect(chart.changingSeries.map(\.latestPoint.snapshotID) == [7])
        #expect(chart.changingSeries.map(\.currentRatio) == [0.99])
        #expect(chart.unchangedSeries.map(\.latestPoint.snapshotID) == [6])
        #expect(chart.unchangedSeries.map(\.currentRatio) == [1])
        #expect(chart.plotPoints.map(\.snapshotID) == [1, 2, 3, 7])
        #expect(chart.segmentMarkers.map(\.snapshotID) == [3, 7])
        #expect(chart.resetMarkers.map(\.snapshotID) == [7])
        #expect(chart.latestMarkers.map(\.snapshotID) == [7])
    }

    @Test("Provider catalog keeps product categories and capability source types canonical")
    func providerCatalog() {
        #expect(ProviderCatalog.descriptor(for: "hermes").category == .localTool)
        #expect(ProviderCatalog.descriptor(for: "openclaw").category == .localTool)
        #expect(ProviderCatalog.descriptor(for: "codex").category == .subscription)
        #expect(ProviderCatalog.descriptor(for: "kiro_cli").category == .subscription)
        #expect(ProviderCatalog.descriptor(for: "cursor").category == .subscription)
        #expect(ProviderCatalog.descriptor(for: "kiro_cli").credentialSourceTypes == [.keychain, .oauth, .none])
        #expect(ProviderCatalog.descriptor(for: "unknown-api").metricFamilies == [.billing, .tokenActivity])
        #expect(ProviderCatalog.descriptor(for: "minimax-1783978290").category == .api)
        #expect(ProviderCatalog.descriptor(for: "step-plan-main").category == .api)
        #expect(ProviderCatalog.descriptor(for: "minimaxevil").category == .api)
        #expect(ProviderCatalog.descriptor(for: "step-planmain").category == .api)
    }

    @Test("Routes accept helper arguments and reject unknown destinations")
    func routeParsing() {
        #expect(UsageDetailsRoute(arguments: ["--route", "health"]) == .dataHealth)
        #expect(UsageDetailsRoute(arguments: ["--route", "capacity"]) == .capacity)
        #expect(UsageDetailsRoute(arguments: ["--route", "automation"]) == .automation)
        #expect(UsageDetailsRoute(arguments: ["--route", "welcome"]) == .activity)
        #expect(UsageDetailsRoute(arguments: ["--route", "overview"]) == .activity)
        #expect(UsageDetailsRoute(arguments: ["--route", "unknown"]) == .activity)
        #expect(UsageDetailsRoute(arguments: ["--route"]) == .activity)
        #expect(UsageDetailsRoute(routeValue: "health") == .dataHealth)
        #expect(UsageDetailsRoute(routeValue: "automation") == .automation)
        #expect(UsageDetailsRoute(routeValue: "unknown") == nil)
        #expect(ActivityRouteMessage.decode(["route": "capacity"]) == .capacity)
        #expect(ActivityRouteMessage.decode(["route": "automation"]) == .automation)
        #expect(ActivityRouteMessage.decode(["route": "health", "token": "secret"]) == nil)
    }

    @Test("Chart selection is deterministic and bounded")
    func chartSelection() {
        #expect(ChartSelection.index(at: -1, plotWidth: 300, count: 30) == nil)
        #expect(ChartSelection.index(at: 0, plotWidth: 300, count: 30) == 0)
        #expect(ChartSelection.index(at: 299, plotWidth: 300, count: 30) == 29)
        #expect(ChartSelection.index(at: 300, plotWidth: 300, count: 30) == nil)
        #expect(ChartSelection.index(at: 1, plotWidth: 0, count: 30) == nil)
    }
}
