import Testing
@testable import UsageCore

@Suite("Activity aggregation")
struct AggregationTests {
    private func day(_ value: String) -> LocalDay { try! LocalDay(value) }

    private func record(
        _ dayValue: String, provider: String = "codex", model: String = "gpt-5.5", tokens: Int64
    ) -> DailyUsage {
        DailyUsage(
            day: day(dayValue), providerID: provider, accountRef: "", modelID: model,
            inputTokens: tokens, outputTokens: 0, cacheReadTokens: 0,
            cacheCreationTokens: 0, reasoningTokens: nil, totalTokens: tokens,
            costAmount: nil, costCurrency: nil, costBasis: nil, quality: "exact",
            importedAt: "2026-07-14T09:00:00Z", revision: 1, recordID: "\(dayValue).\(provider).\(model)"
        )
    }

    private func coverage(_ value: String, provider: String = "codex", covered: Bool = true) -> CoverageDay {
        CoverageDay(day: day(value), providerID: provider, accountRef: "", isCovered: covered)
    }

    @Test("Covered zero and missing remain different and break streaks")
    func coveredZeroVersusMissing() {
        let data = ActivityDataset(
            records: [record("2026-07-01", tokens: 10), record("2026-07-04", tokens: 20)],
            coverage: [
                coverage("2026-07-01"), coverage("2026-07-02"),
                coverage("2026-07-03", covered: false), coverage("2026-07-04"),
            ], knownScopes: [ProviderScope(providerID: "codex", accountRef: "")], revision: 1842
        )
        let view = ActivityAggregator.makeViewModel(from: data)
        #expect(view.days.map(\.totalTokens) == [10, 0, nil, 20])
        #expect(view.days.map(\.state) == [.coveredActive, .coveredZero, .missing, .coveredActive])
        #expect(view.days.map(\.heatLevel) == [3, 0, nil, 5])
        #expect(view.metrics.totalTokens == nil)
        #expect(view.metrics.observedTokens == 30)
        #expect(!view.metrics.isComplete)
        #expect(view.metrics.peak == PeakUsage(day: day("2026-07-04"), tokens: 20))
        #expect(view.metrics.activeDays == 2)
        #expect(view.metrics.currentStreak == 1)
        #expect(view.metrics.longestStreak == 1)
    }

    @Test("Heat quintiles are deterministic and never collapse zero into missing")
    func heatDistribution() {
        let values: [Int64] = [1, 2, 3, 4, 5]
        let records = values.enumerated().map { index, tokens in
            record("2026-07-0\(index + 1)", tokens: tokens)
        }
        let coverageRows = values.indices.map { coverage("2026-07-0\($0 + 1)") }
        let data = ActivityDataset(records: records, coverage: coverageRows, knownScopes: [], revision: 1)
        #expect(ActivityAggregator.makeViewModel(from: data).days.map(\.heatLevel) == [1, 2, 3, 4, 5])
    }

    @Test("Provider and model filters recompute totals and coverage")
    func filtersRecompute() {
        let data = ActivityDataset(
            records: [
                record("2026-07-01", provider: "codex", model: "gpt-5.5", tokens: 10),
                record("2026-07-01", provider: "minimax", model: "minimax-m2", tokens: 40),
            ],
            coverage: [coverage("2026-07-01"), coverage("2026-07-01", provider: "minimax")],
            knownScopes: [], revision: 1
        )
        let providerView = ActivityAggregator.makeViewModel(from: data, providerIDs: ["minimax"])
        #expect(providerView.metrics.totalTokens == 40)
        #expect(providerView.days.map(\.totalTokens) == [40])
        let modelView = ActivityAggregator.makeViewModel(from: data, modelIDs: ["gpt-5.5"])
        #expect(modelView.metrics.totalTokens == 10)
    }

    @Test("Partial provider coverage is explicit and a provider filter restores completeness")
    func partialCoverage() {
        let data = ActivityDataset(
            records: [record("2026-07-01", provider: "codex", model: "gpt-5.5", tokens: 10)],
            coverage: [
                coverage("2026-07-01", provider: "codex"),
                coverage("2026-07-01", provider: "minimax", covered: false),
            ],
            knownScopes: [
                ProviderScope(providerID: "codex", accountRef: ""),
                ProviderScope(providerID: "minimax", accountRef: ""),
            ],
            revision: 1
        )

        let combined = ActivityAggregator.makeViewModel(from: data)
        let partial = combined.days[0]
        #expect(partial.state == .partial)
        #expect(partial.totalTokens == nil)
        #expect(partial.observedTokens == 10)
        #expect(partial.heatLevel == 5)
        #expect(combined.metrics.totalTokens == nil)
        #expect(combined.metrics.observedTokens == 10)
        #expect(!combined.metrics.isComplete)
        #expect(combined.metrics.peak == PeakUsage(day: day("2026-07-01"), tokens: 10))
        #expect(combined.metrics.activeDays == 1)
        #expect(combined.metrics.currentStreak == 1)
        #expect(combined.metrics.longestStreak == 1)
        #expect(combined.modelComposition30Days.map(\.tokens) == [10])
        #expect(!combined.modelCompositionIsComplete)

        let codex = ActivityAggregator.makeViewModel(from: data, providerIDs: ["codex"])
        #expect(codex.days[0].state == .coveredActive)
        #expect(codex.days[0].totalTokens == 10)
        #expect(codex.days[0].observedTokens == 10)
        #expect(codex.days[0].heatLevel == 5)
        #expect(codex.metrics.totalTokens == 10)
        #expect(codex.metrics.isComplete)
        #expect(codex.metrics.currentStreak == 1)
        #expect(codex.modelCompositionIsComplete)

        let minimax = ActivityAggregator.makeViewModel(from: data, providerIDs: ["minimax"])
        #expect(minimax.days[0].state == .missing)
        #expect(minimax.days[0].totalTokens == nil)
        #expect(minimax.days[0].observedTokens == 0)
        #expect(!minimax.metrics.isComplete)
    }

    @Test("A canonical usage row is covered evidence even if a caller omits coverage metadata")
    func usageRowImpliesCoverage() {
        let data = ActivityDataset(
            records: [record("2026-07-01", tokens: 12)], coverage: [], knownScopes: [], revision: 1
        )
        let day = ActivityAggregator.makeViewModel(from: data).days.first
        #expect(day?.totalTokens == 12)
        #expect(day?.heatLevel == 5)
    }

    @Test("Twelve named models plus explicit overflow conserves exact totals")
    func namedModelsPlusOverflow() {
        let models = (1...14).map { "m\($0)" }
        let records = models.map { record("2026-07-01", model: $0, tokens: 10) }
        let data = ActivityDataset(
            records: records, coverage: [coverage("2026-07-01")], knownScopes: [], revision: 1
        )
        let composition = ActivityAggregator.makeViewModel(from: data).modelComposition30Days
        #expect(composition.count == 13)
        #expect(composition.last?.modelID == "additional-models")
        #expect(composition.last?.tokens == 20)
        let compositionTotal = composition.reduce(Int64(0)) { $0 + $1.tokens }
        let recordTotal = records.reduce(Int64(0)) { $0 + $1.totalTokens }
        #expect(compositionTotal == recordTotal)
    }

    @Test("Capacity urgency is stable and stale remains visible")
    func capacityViewModel() {
        let rows = [
            CapacityItem.stub(recordID: "b", providerID: "b", remainingRatio: nil, stale: true),
            CapacityItem.stub(recordID: "a2", providerID: "a", remainingRatio: 0.8, stale: false),
            CapacityItem.stub(recordID: "a1", providerID: "a", remainingRatio: 0.2, stale: true),
            CapacityItem.stub(recordID: "c", providerID: "c", remainingRatio: 0.0, stale: false),
        ]
        let sorted = CapacityViewModel.sorted(rows)
        #expect(sorted.map(\.recordID) == ["c", "a1", "a2", "b"])
        #expect(sorted[1].stale)
    }
}

private extension CapacityItem {
    static func stub(
        recordID: String, providerID: String, remainingRatio: Double?, stale: Bool
    ) -> CapacityItem {
        CapacityItem(
            recordID: recordID, providerID: providerID, accountRef: "", quotaName: "quota",
            unit: "tokens", used: nil, limit: nil, remaining: nil,
            remainingRatio: remainingRatio, resetsAt: nil, periodStart: nil, periodEnd: nil,
            observedAt: "2026-07-14T09:00:00Z", freshnessSeconds: 0, state: "ok",
            quality: "live", stale: stale, revision: 1
        )
    }
}
