import Foundation

public struct PeakUsage: Sendable, Hashable {
    public let day: LocalDay
    public let tokens: Int64

    public init(day: LocalDay, tokens: Int64) {
        self.day = day
        self.tokens = tokens
    }
}

/// Raw Token counters reported by a usage source. Cache counters are kept
/// separate because cache reads can already be included in input Tokens.
public struct TokenBreakdown: Sendable, Hashable {
    public let totalTokens: Int64
    public let inputTokens: Int64
    public let outputTokens: Int64
    public let cacheReadTokens: Int64
    public let cacheCreationTokens: Int64

    public init(
        totalTokens: Int64, inputTokens: Int64, outputTokens: Int64,
        cacheReadTokens: Int64, cacheCreationTokens: Int64
    ) {
        self.totalTokens = totalTokens
        self.inputTokens = inputTokens
        self.outputTokens = outputTokens
        self.cacheReadTokens = cacheReadTokens
        self.cacheCreationTokens = cacheCreationTokens
    }

    public static let zero = TokenBreakdown(
        totalTokens: 0, inputTokens: 0, outputTokens: 0,
        cacheReadTokens: 0, cacheCreationTokens: 0
    )

    init(records: [DailyUsage]) {
        var totalTokens: Int64 = 0
        var inputTokens: Int64 = 0
        var outputTokens: Int64 = 0
        var cacheReadTokens: Int64 = 0
        var cacheCreationTokens: Int64 = 0
        for record in records {
            totalTokens += record.totalTokens
            inputTokens += record.inputTokens
            outputTokens += record.outputTokens
            cacheReadTokens += record.cacheReadTokens
            cacheCreationTokens += record.cacheCreationTokens
        }
        self.init(
            totalTokens: totalTokens, inputTokens: inputTokens, outputTokens: outputTokens,
            cacheReadTokens: cacheReadTokens, cacheCreationTokens: cacheCreationTokens
        )
    }
}

public struct ActivityMetrics: Sendable, Hashable {
    /// Exact total when every selected provider/account scope is covered.
    public let totalTokens: Int64?
    /// Exact sum of the records that were observed, even when coverage is partial.
    public let observedTokens: Int64
    /// Observed component counters. These facts are not assumed to be additive.
    public let observedBreakdown: TokenBreakdown
    public let isComplete: Bool
    public let peak: PeakUsage?
    public let activeDays: Int
    public let currentStreak: Int
    public let longestStreak: Int

    public var hasObservedBreakdown: Bool {
        isComplete || observedBreakdown != .zero
    }
}

public enum ActivityDayState: String, Sendable, Hashable {
    case missing
    case partial
    case coveredZero
    case coveredActive
}

public struct ActivityDay: Sendable, Hashable {
    public let day: LocalDay
    public let state: ActivityDayState
    /// Nil for missing or partial days because their complete total is unknown.
    public let totalTokens: Int64?
    public let observedTokens: Int64
    public let heatLevel: Int?

    public init(
        day: LocalDay, state: ActivityDayState, totalTokens: Int64?,
        observedTokens: Int64, heatLevel: Int?
    ) {
        self.day = day
        self.state = state
        self.totalTokens = totalTokens
        self.observedTokens = observedTokens
        self.heatLevel = heatLevel
    }
}

public struct ModelComposition: Sendable, Hashable {
    public let modelID: String
    public let tokens: Int64
}

public struct ActivityViewModel: Sendable, Hashable {
    public let days: [ActivityDay]
    public let metrics: ActivityMetrics
    public let modelComposition30Days: [ModelComposition]
    public let modelCompositionIsComplete: Bool
    public let revision: Int64
}

public enum CapacityViewModel {
    public static func sorted(_ rows: [CapacityItem]) -> [CapacityItem] {
        rows.sorted(by: isMoreUrgent)
    }

    static func isMoreUrgent(_ lhs: CapacityItem, _ rhs: CapacityItem) -> Bool {
        switch (lhs.remainingRatio, rhs.remainingRatio) {
        case let (left?, right?) where left != right: return left < right
        case (_?, nil): return true
        case (nil, _?): return false
        default:
            if lhs.providerID != rhs.providerID { return lhs.providerID < rhs.providerID }
            if lhs.accountRef != rhs.accountRef { return lhs.accountRef < rhs.accountRef }
            if lhs.quotaName != rhs.quotaName { return lhs.quotaName < rhs.quotaName }
            return lhs.recordID < rhs.recordID
        }
    }
}

public enum ActivityAggregator {
    /// Positive observed days are assigned empirical quintiles. Rank uses the upper
    /// bound for equal values, making ties stable. Partial days keep their observed
    /// intensity while exact totals remain nil. Covered zero is level 0; missing is nil.
    public static func makeViewModel(
        from dataset: ActivityDataset,
        providerIDs: Set<String> = [],
        modelIDs: Set<String> = []
    ) -> ActivityViewModel {
        let selectedRecords = dataset.records.filter { record in
            (providerIDs.isEmpty || providerIDs.contains(record.providerID))
                && (modelIDs.isEmpty || modelIDs.contains(record.modelID))
        }
        let selectedCoverage = dataset.coverage.filter {
            providerIDs.isEmpty || providerIDs.contains($0.providerID)
        }
        let allDays = Set(selectedCoverage.map(\.day)).union(selectedRecords.map(\.day)).sorted()
        let totals = Dictionary(grouping: selectedRecords, by: \.day).mapValues {
            $0.reduce(Int64(0)) { $0 + $1.totalTokens }
        }
        let selectedKnownScopes = dataset.knownScopes.filter {
            providerIDs.isEmpty || providerIDs.contains($0.providerID)
        }
        let coverageScopes = selectedCoverage.map {
            ProviderScope(providerID: $0.providerID, accountRef: $0.accountRef)
        }
        let recordScopes = selectedRecords.map {
            ProviderScope(providerID: $0.providerID, accountRef: $0.accountRef)
        }
        let expectedScopes = Set(selectedKnownScopes).union(coverageScopes).union(recordScopes)
        let coveredByDay = Dictionary(grouping: selectedCoverage.filter(\.isCovered), by: \.day).mapValues {
            Set($0.map { ProviderScope(providerID: $0.providerID, accountRef: $0.accountRef) })
        }
        let recordScopesByDay = Dictionary(grouping: selectedRecords, by: \.day).mapValues {
            Set($0.map { ProviderScope(providerID: $0.providerID, accountRef: $0.accountRef) })
        }

        let facts: [(day: LocalDay, state: ActivityDayState, observed: Int64, total: Int64?)] = allDays.map { day in
            let observed = totals[day] ?? 0
            let covered = (coveredByDay[day] ?? []).union(recordScopesByDay[day] ?? [])
            if !expectedScopes.isEmpty, expectedScopes.isSubset(of: covered) {
                let state: ActivityDayState = observed > 0 ? .coveredActive : .coveredZero
                return (day, state, observed, observed)
            }
            if covered.isEmpty { return (day, .missing, observed, nil) }
            return (day, .partial, observed, nil)
        }
        let positive = facts.map(\.observed).filter { $0 > 0 }.sorted()

        func heat(for total: Int64?) -> Int? {
            guard let total else { return nil }
            guard total > 0 else { return 0 }
            let upperRank = positive.partitioningIndex { $0 > total }
            return max(1, min(5, (upperRank * 5 + positive.count - 1) / positive.count))
        }

        let days = facts.map { fact -> ActivityDay in
            let intensityTokens: Int64? = fact.observed > 0 ? fact.observed : fact.total
            return ActivityDay(
                day: fact.day, state: fact.state, totalTokens: fact.total,
                observedTokens: fact.observed, heatLevel: heat(for: intensityTokens)
            )
        }

        let observedBreakdown = TokenBreakdown(records: selectedRecords)
        let observedTokens = observedBreakdown.totalTokens
        let isComplete = !days.isEmpty && days.allSatisfy {
            $0.state == .coveredZero || $0.state == .coveredActive
        }
        let observedActiveDays = days.filter { $0.observedTokens > 0 }
        let peak = observedActiveDays.max {
            let left = $0.observedTokens
            let right = $1.observedTokens
            return left == right ? $0.day > $1.day : left < right
        }.map { PeakUsage(day: $0.day, tokens: $0.observedTokens) }
        var longest = 0
        var running = 0
        for day in days {
            if day.observedTokens > 0 {
                running += 1
                longest = max(longest, running)
            } else {
                running = 0
            }
        }
        let metrics = ActivityMetrics(
            totalTokens: isComplete ? observedTokens : nil,
            observedTokens: observedTokens, observedBreakdown: observedBreakdown,
            isComplete: isComplete,
            peak: peak, activeDays: observedActiveDays.count,
            currentStreak: running, longestStreak: longest
        )

        let compositionStart: LocalDay? = allDays.last.flatMap { lastDay in
            guard let date = lastDay.utcDate else { return nil }
            return Calendar.utc.date(byAdding: .day, value: -29, to: date).flatMap(LocalDay.init(date:))
        }
        let compositionRecords = selectedRecords.filter { record in
            guard let compositionStart else { return true }
            return record.day >= compositionStart
        }
        let byModel = Dictionary(grouping: compositionRecords, by: ModelSeriesIdentity.id).mapValues {
            $0.reduce(Int64(0)) { $0 + $1.totalTokens }
        }
        let unsortedComposition: [ModelComposition] = byModel.map { key, value in
            ModelComposition(modelID: key, tokens: value)
        }
        let ranked = unsortedComposition.sorted { left, right in
            if left.tokens == right.tokens { return left.modelID < right.modelID }
            return left.tokens > right.tokens
        }
        var composition = Array(ranked.prefix(ModelSeriesIdentity.namedLimit))
        let overflowTotal = ranked.dropFirst(ModelSeriesIdentity.namedLimit)
            .reduce(Int64(0)) { $0 + $1.tokens }
        if overflowTotal > 0 {
            composition.append(ModelComposition(
                modelID: ModelSeriesIdentity.overflowID, tokens: overflowTotal
            ))
        }

        let compositionDays = days.filter { day in
            guard let compositionStart else { return true }
            return day.day >= compositionStart
        }
        let compositionIsComplete = !compositionDays.isEmpty && compositionDays.allSatisfy {
            $0.state == .coveredZero || $0.state == .coveredActive
        }

        return ActivityViewModel(
            days: days, metrics: metrics, modelComposition30Days: composition,
            modelCompositionIsComplete: compositionIsComplete,
            revision: dataset.revision
        )
    }
}

private extension Array where Element == Int64 {
    func partitioningIndex(where predicate: (Int64) -> Bool) -> Int {
        var low = startIndex
        var high = endIndex
        while low < high {
            let middle = low + (high - low) / 2
            if predicate(self[middle]) { high = middle } else { low = middle + 1 }
        }
        return low
    }
}

extension LocalDay {
    var utcDate: Date? {
        let parts = rawValue.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return nil }
        return Calendar.utc.date(from: DateComponents(year: parts[0], month: parts[1], day: parts[2]))
    }

    init?(date: Date) {
        let parts = Calendar.utc.dateComponents([.year, .month, .day], from: date)
        guard let year = parts.year, let month = parts.month, let day = parts.day else { return nil }
        try? self.init(String(format: "%04d-%02d-%02d", year, month, day))
    }
}

extension Calendar {
    static var utc: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        return calendar
    }
}
