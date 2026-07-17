import Foundation

public enum UsagePeriod: String, CaseIterable, Sendable, Hashable {
    case day, week, month, year

    public func range(ending end: LocalDay) -> ClosedRange<LocalDay> {
        let length = switch self {
        case .day: 1
        case .week: 7
        case .month: 30
        case .year: 365
        }
        return Self.shiftedRange(ending: end, length: length)
    }

    public func repositoryRange(ending end: LocalDay) -> ClosedRange<LocalDay> {
        range(ending: end)
    }

    public static func modelTrendRange(ending end: LocalDay) -> ClosedRange<LocalDay> {
        shiftedRange(ending: end, length: 30)
    }

    private static func shiftedRange(ending end: LocalDay, length: Int) -> ClosedRange<LocalDay> {
        guard let date = end.utcDate,
              let startDate = Calendar.utc.date(byAdding: .day, value: 1 - length, to: date),
              let start = LocalDay(date: startDate)
        else { return end...end }
        return start...end
    }
}

public extension ClosedRange where Bound == LocalDay {
    var dayCount: Int {
        guard let start = lowerBound.utcDate, let end = upperBound.utcDate else { return 0 }
        return (Calendar.utc.dateComponents([.day], from: start, to: end).day ?? -1) + 1
    }

    var days: [LocalDay] {
        guard let start = lowerBound.utcDate, let end = upperBound.utcDate else { return [] }
        var result: [LocalDay] = []
        var cursor = start
        while cursor <= end {
            if let day = LocalDay(date: cursor) { result.append(day) }
            guard let next = Calendar.utc.date(byAdding: .day, value: 1, to: cursor) else { break }
            cursor = next
        }
        return result
    }
}

public enum ActivityQuality: String, Sendable, Hashable {
    case exact, estimated, partial, missing
}

public struct DailyModelPoint: Identifiable, Sendable, Hashable {
    public let day: LocalDay
    public let modelID: String
    public let tokens: Int64
    public var id: String { "\(day.rawValue)|\(modelID)" }
}

enum ModelSeriesIdentity {
    static let namedLimit = 12
    static let overflowID = "additional-models"
    private static let unattributedPrefix = "unattributed:"

    static func id(for record: DailyUsage) -> String {
        record.modelID == "unknown"
            ? unattributedPrefix + record.providerID
            : record.modelID
    }

    static func unattributedProviderID(from id: String) -> String? {
        guard id.hasPrefix(unattributedPrefix) else { return nil }
        let providerID = String(id.dropFirst(unattributedPrefix.count))
        return providerID.isEmpty ? nil : providerID
    }
}

public struct DailyChartDay: Identifiable, Sendable, Hashable {
    public let day: LocalDay
    public let state: ActivityDayState
    public let totalTokens: Int64?
    public let observedTokens: Int64
    public let composition: [ModelComposition]
    public let quality: ActivityQuality
    public var id: LocalDay { day }

    public var accessibilitySummary: String {
        let total = totalTokens.map(TokenText.compact) ?? "Unavailable"
        let models = composition.map {
            "\(DisplayText.model($0.modelID)), \(TokenText.compact($0.tokens))"
        }.joined(separator: ", ")
        return [day.rawValue, "\(total) Tokens", models, quality.displayName]
            .filter { !$0.isEmpty }.joined(separator: ", ")
    }
}

public struct ModelSeriesVisibility: Sendable, Hashable {
    public private(set) var hiddenSeriesIDs: Set<String>

    public init(hiddenSeriesIDs: Set<String> = []) {
        self.hiddenSeriesIDs = hiddenSeriesIDs
    }

    @discardableResult
    public mutating func toggle(
        _ seriesID: String, availableSeriesIDs: [String]
    ) -> Bool {
        guard availableSeriesIDs.contains(seriesID) else { return false }
        if hiddenSeriesIDs.contains(seriesID) {
            hiddenSeriesIDs.remove(seriesID)
        } else {
            hiddenSeriesIDs.insert(seriesID)
        }
        return true
    }

    public mutating func reconcile(availableSeriesIDs: [String]) {
        hiddenSeriesIDs.formIntersection(availableSeriesIDs)
    }

    public mutating func showAll() {
        hiddenSeriesIDs.removeAll()
    }

    public func isVisible(_ seriesID: String) -> Bool {
        !hiddenSeriesIDs.contains(seriesID)
    }
}

public struct ModelSeriesDescriptor: Identifiable, Sendable, Hashable {
    public let modelID: String
    public let styleIndex: Int
    public let isVisible: Bool
    public var id: String { modelID }
}

public enum ModelSeriesDisplayState: Sendable, Hashable {
    case noSeries
    case visible
    case allHidden
}

public struct ModelSeriesPresentation: Sendable, Hashable {
    public let series: [ModelSeriesDescriptor]
    public let seriesPoints: [DailyModelPoint]
    public let chartDays: [DailyChartDay]

    public var visibleSeriesIDs: [String] {
        series.filter(\.isVisible).map(\.modelID)
    }

    public var displayState: ModelSeriesDisplayState {
        if series.isEmpty { return .noSeries }
        return visibleSeriesIDs.isEmpty ? .allHidden : .visible
    }
}

public struct HeatmapDayDetail: Identifiable, Sendable, Hashable {
    public let activity: ActivityDay
    public let quality: ActivityQuality
    public let lastCollectionAt: String?
    public let isStale: Bool
    public var id: LocalDay { activity.day }

    public init(
        activity: ActivityDay, quality: ActivityQuality,
        lastCollectionAt: String?, isStale: Bool = false
    ) {
        self.activity = activity
        self.quality = quality
        self.lastCollectionAt = lastCollectionAt
        self.isStale = isStale
    }

    public var accessibilitySummary: String {
        let value = switch activity.state {
        case .missing: "Missing collection data"
        case .partial: "Observed \(TokenText.compact(activity.observedTokens)) Tokens, Partial"
        case .coveredZero: "Covered with zero Tokens"
        case .coveredActive: "\(TokenText.compact(activity.totalTokens ?? 0)) Tokens"
        }
        return [
            activity.day.rawValue, value, quality.displayName,
            isStale ? "Stale" : nil,
            lastCollectionAt.map { "Last collection \($0)" },
        ].compactMap { $0 }.joined(separator: ", ")
    }
}

public struct HeatmapCalendarSlot: Identifiable, Sendable, Hashable {
    public let position: Int
    public let row: Int
    public let column: Int
    public let detail: HeatmapDayDetail?
    public var id: Int { position }
}

public struct HeatmapMonthAnchor: Sendable, Hashable {
    public let key: String
    public let column: Int
}

public struct HeatmapCalendarLayout: Sendable, Hashable {
    public let slots: [HeatmapCalendarSlot]
    public let columnCount: Int
    public let monthAnchors: [HeatmapMonthAnchor]

    public init(range: ClosedRange<LocalDay>, details: [HeatmapDayDetail]) {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        calendar.firstWeekday = 1
        let firstDate = range.lowerBound.utcDate
        let leading = firstDate.map { calendar.component(.weekday, from: $0) - calendar.firstWeekday } ?? 0
        let dayDetails = Dictionary(uniqueKeysWithValues: details.map { ($0.activity.day, $0) })
        let days = range.days
        let unpaddedCount = leading + days.count
        let columns = max(1, Int(ceil(Double(unpaddedCount) / 7.0)))
        let count = columns * 7
        self.columnCount = columns
        self.slots = (0..<count).map { position in
            let dayIndex = position - leading
            let detail = days.indices.contains(dayIndex) ? dayDetails[days[dayIndex]] : nil
            return HeatmapCalendarSlot(
                position: position, row: position % 7, column: position / 7, detail: detail
            )
        }
        var anchors: [HeatmapMonthAnchor] = []
        for (index, day) in days.enumerated() {
            let key = String(day.rawValue.prefix(7))
            guard anchors.last?.key != key else { continue }
            anchors.append(HeatmapMonthAnchor(key: key, column: (leading + index) / 7))
        }
        self.monthAnchors = anchors
    }

    public var accessibilityDayCount: Int { slots.count { $0.detail != nil } }

    public func destination(from position: Int, direction: HeatmapDirection) -> Int {
        guard slots.indices.contains(position), slots[position].detail != nil else { return position }
        let delta = switch direction {
        case .up: -1
        case .down: 1
        case .left: -7
        case .right: 7
        }
        let target = position + delta
        guard slots.indices.contains(target), slots[target].detail != nil else { return position }
        return target
    }

    public static func x(forColumn column: Int, pitch: Double) -> Double {
        Double(column) * pitch
    }
}

public struct UsageDetailsModel: Sendable, Hashable {
    public let heatmapDays: [ActivityDay]
    public let heatmapDetails: [HeatmapDayDetail]
    public let metrics: ActivityMetrics
    public let seriesPoints: [DailyModelPoint]
    public let chartDays: [DailyChartDay]
    public let providerIDs: [String]
    public let modelIDs: [String]
    public let filterSignature: String
    public let revision: Int64

    public static func empty(filterSignature: String, revision: Int64) -> Self {
        Self(
            heatmapDays: [], heatmapDetails: [],
            metrics: ActivityMetrics(
                totalTokens: nil, observedTokens: 0, isComplete: false,
                peak: nil, activeDays: 0, currentStreak: 0, longestStreak: 0
            ),
            seriesPoints: [], chartDays: [], providerIDs: [], modelIDs: [],
            filterSignature: filterSignature, revision: revision
        )
    }

    public var modelSeriesIDs: [String] {
        var seen: Set<String> = []
        return seriesPoints.compactMap { point in
            seen.insert(point.modelID).inserted ? point.modelID : nil
        }
    }

    public func modelSeriesPresentation(
        visibility: ModelSeriesVisibility = ModelSeriesVisibility()
    ) -> ModelSeriesPresentation {
        let series = modelSeriesIDs.enumerated().map { index, modelID in
            ModelSeriesDescriptor(
                modelID: modelID,
                styleIndex: index,
                isVisible: visibility.isVisible(modelID)
            )
        }
        let visible = Set(series.filter(\.isVisible).map(\.modelID))
        let points = seriesPoints.filter { visible.contains($0.modelID) }
        let days = chartDays.map { day in
            DailyChartDay(
                day: day.day,
                state: day.state,
                totalTokens: day.totalTokens,
                observedTokens: day.observedTokens,
                composition: day.composition.filter { visible.contains($0.modelID) },
                quality: day.quality
            )
        }
        return ModelSeriesPresentation(series: series, seriesPoints: points, chartDays: days)
    }
}

public enum QuotaHistorySegmentStart: Sendable, Hashable {
    case initial
    case continuation
    case quotaWindow
    case samplingGap
}

public struct QuotaHistoryPoint: Identifiable, Sendable, Hashable {
    public let snapshotID: Int64
    public let observedAt: Date
    public let day: LocalDay
    public let remainingRatio: Double
    public let seriesID: String
    public let lineSegmentID: String
    public let styleKey: String
    public let seriesLabel: String
    public let resetsAt: Date?
    public let state: String
    public let stale: Bool
    public let segmentStart: QuotaHistorySegmentStart
    public var id: Int64 { snapshotID }

    public init(
        snapshotID: Int64, observedAt: Date, day: LocalDay,
        remainingRatio: Double, seriesID: String, lineSegmentID: String,
        styleKey: String, seriesLabel: String, resetsAt: Date?,
        state: String, stale: Bool,
        segmentStart: QuotaHistorySegmentStart = .initial
    ) {
        self.snapshotID = snapshotID
        self.observedAt = observedAt
        self.day = day
        self.remainingRatio = remainingRatio
        self.seriesID = seriesID
        self.lineSegmentID = lineSegmentID
        self.styleKey = styleKey
        self.seriesLabel = seriesLabel
        self.resetsAt = resetsAt
        self.state = state
        self.stale = stale
        self.segmentStart = segmentStart
    }
}

public struct QuotaHistorySeriesPresentation: Identifiable, Sendable, Hashable {
    public let seriesID: String
    public let styleKey: String
    public let seriesLabel: String
    public let points: [QuotaHistoryPoint]
    public let latestPoint: QuotaHistoryPoint

    public var id: String { seriesID }
    public var currentRatio: Double { latestPoint.remainingRatio }
    public var isChanging: Bool {
        guard let first = points.first else { return false }
        let displayed = Int((first.remainingRatio * 1_000).rounded())
        return points.dropFirst().contains {
            Int(($0.remainingRatio * 1_000).rounded()) != displayed
        }
    }
}

public struct QuotaHistoryChartPresentation: Sendable, Hashable {
    public let series: [QuotaHistorySeriesPresentation]

    public init(points: [QuotaHistoryPoint]) {
        series = Dictionary(grouping: points, by: \.seriesID)
            .compactMap { seriesID, values in
                let ordered = values.sorted {
                    $0.observedAt == $1.observedAt
                        ? $0.snapshotID < $1.snapshotID
                        : $0.observedAt < $1.observedAt
                }
                guard let first = ordered.first, let latest = ordered.last else { return nil }
                return QuotaHistorySeriesPresentation(
                    seriesID: seriesID, styleKey: first.styleKey,
                    seriesLabel: first.seriesLabel, points: ordered,
                    latestPoint: latest
                )
            }
            .sorted {
                $0.seriesLabel == $1.seriesLabel
                    ? $0.seriesID < $1.seriesID
                    : $0.seriesLabel < $1.seriesLabel
            }
    }

    public var changingSeries: [QuotaHistorySeriesPresentation] {
        series.filter(\.isChanging)
    }

    public var unchangedSeries: [QuotaHistorySeriesPresentation] {
        series.filter { !$0.isChanging }
    }

    public var plotPoints: [QuotaHistoryPoint] {
        changingSeries.flatMap(\.points)
    }

    public var segmentMarkers: [QuotaHistoryPoint] {
        changingSeries.flatMap { series in
            Dictionary(grouping: series.points, by: \.lineSegmentID).values.compactMap { points in
                points.max {
                    $0.observedAt == $1.observedAt
                        ? $0.snapshotID < $1.snapshotID
                        : $0.observedAt < $1.observedAt
                }
            }
        }
        .sorted {
            $0.observedAt == $1.observedAt
                ? $0.snapshotID < $1.snapshotID
                : $0.observedAt < $1.observedAt
        }
    }

    public var resetMarkers: [QuotaHistoryPoint] {
        changingSeries.flatMap(\.points)
            .filter { $0.segmentStart == .quotaWindow }
            .sorted(by: isEarlier)
    }

    public var latestMarkers: [QuotaHistoryPoint] {
        changingSeries.map(\.latestPoint)
    }

    public var isolatedSegmentMarkers: [QuotaHistoryPoint] {
        changingSeries.flatMap { series in
            Dictionary(grouping: series.points, by: \.lineSegmentID).values.compactMap {
                $0.count == 1 ? $0[0] : nil
            }
        }
        .sorted(by: isEarlier)
    }

    private func isEarlier(_ left: QuotaHistoryPoint, _ right: QuotaHistoryPoint) -> Bool {
        left.observedAt == right.observedAt
            ? left.snapshotID < right.snapshotID
            : left.observedAt < right.observedAt
    }
}

public enum QuotaHistoryPresentation {
    private static let materialReplenishmentTenths = 50
    private static let resetDriftTolerance: TimeInterval = 90

    public static func make(
        items: [QuotaHistoryItem], calendar: Calendar,
        providerDescriptors: [String: ProviderDisplayDescriptor] = [:]
    ) throws -> [QuotaHistoryPoint] {
        let points = try items.compactMap { item -> QuotaHistoryPoint? in
            guard let ratio = item.remainingRatio else { return nil }
            guard let date = parseISO8601(item.observedAt) else {
                throw LocalDayError.invalidFormat
            }
            let resetsAt: Date?
            if let value = item.resetsAt {
                guard let parsed = parseISO8601(value) else { throw LocalDayError.invalidFormat }
                resetsAt = parsed
            } else {
                resetsAt = nil
            }
            let components = calendar.dateComponents([.year, .month, .day], from: date)
            guard let year = components.year, let month = components.month, let day = components.day else {
                throw LocalDayError.invalidFormat
            }
            let localDay = try LocalDay(String(format: "%04d-%02d-%02d", year, month, day))
            let seriesID = item.seriesID
            let account = item.accountRef.isEmpty ? "" : " · \(pseudonymousAccount(item.accountRef))"
            return QuotaHistoryPoint(
                snapshotID: item.snapshotID, observedAt: date, day: localDay,
                remainingRatio: ratio, seriesID: seriesID,
                lineSegmentID: "Window \(stableHash(seriesID))",
                styleKey: "Series \(stableHash(seriesID))",
                seriesLabel: "\(providerDescriptors[item.providerID]?.displayName ?? DisplayText.provider(item.providerID)) · \(item.quotaName)\(account)",
                resetsAt: resetsAt, state: item.state, stale: item.stale
            )
        }
        .sorted {
            $0.observedAt == $1.observedAt
                ? $0.snapshotID < $1.snapshotID
                : $0.observedAt < $1.observedAt
        }
        return splitSamplingGaps(splitMaterialReplenishments(points))
    }

    private static func splitMaterialReplenishments(
        _ points: [QuotaHistoryPoint]
    ) -> [QuotaHistoryPoint] {
        var previous: [String: (lastReset: Date?, ratio: Double, awaitingReset: Bool)] = [:]
        var generation: [String: Int] = [:]
        return points.map { point in
            let prior = previous[point.seriesID]
            var startsNewWindow = false
            if let prior {
                let delta = Int((point.remainingRatio * 1_000).rounded())
                    - Int((prior.ratio * 1_000).rounded())
                let replenished = delta >= materialReplenishmentTenths
                if (!prior.awaitingReset
                    && resetChanged(from: prior.lastReset, to: point.resetsAt))
                    || replenished {
                    generation[point.seriesID, default: 0] += 1
                    startsNewWindow = true
                }
                let awaitingReset = point.resetsAt == nil
                    && (prior.awaitingReset || replenished)
                previous[point.seriesID] = (
                    awaitingReset ? nil : (point.resetsAt ?? prior.lastReset),
                    point.remainingRatio,
                    awaitingReset
                )
            } else {
                previous[point.seriesID] = (
                    point.resetsAt, point.remainingRatio, false
                )
            }
            let segment = generation[point.seriesID, default: 0]
            return QuotaHistoryPoint(
                snapshotID: point.snapshotID, observedAt: point.observedAt, day: point.day,
                remainingRatio: point.remainingRatio, seriesID: point.seriesID,
                lineSegmentID: "Window \(stableHash(point.seriesID + "|\(segment)"))",
                styleKey: point.styleKey, seriesLabel: point.seriesLabel,
                resetsAt: point.resetsAt, state: point.state, stale: point.stale,
                segmentStart: prior == nil ? .initial
                    : (startsNewWindow ? .quotaWindow : .continuation)
            )
        }
    }

    private static func splitSamplingGaps(
        _ points: [QuotaHistoryPoint]
    ) -> [QuotaHistoryPoint] {
        var replacements: [Int64: QuotaHistoryPoint] = [:]
        for seriesPoints in Dictionary(grouping: points, by: \.seriesID).values {
            for windowPoints in Dictionary(grouping: seriesPoints, by: \.lineSegmentID).values {
                let ordered = windowPoints.sorted {
                    $0.observedAt == $1.observedAt
                        ? $0.snapshotID < $1.snapshotID
                        : $0.observedAt < $1.observedAt
                }
                var gapGeneration = 0
                var recentIntervals: [TimeInterval] = []
                for (index, point) in ordered.enumerated() {
                    let interval = index > 0
                        ? point.observedAt.timeIntervalSince(ordered[index - 1].observedAt)
                        : 0
                    let beginsGap: Bool
                    if interval > 0, !recentIntervals.isEmpty {
                        let sorted = recentIntervals.sorted()
                        let cadence = sorted[(sorted.count - 1) / 2]
                        let threshold = max(cadence * 3, cadence + 900)
                        beginsGap = interval > threshold
                    } else {
                        beginsGap = false
                    }
                    if beginsGap {
                        gapGeneration += 1
                        recentIntervals.removeAll(keepingCapacity: true)
                    } else if interval > 0 {
                        recentIntervals.append(interval)
                        if recentIntervals.count > 7 { recentIntervals.removeFirst() }
                    }
                    let segmentID = gapGeneration == 0
                        ? point.lineSegmentID
                        : "Window \(stableHash(point.lineSegmentID + "|gap:\(gapGeneration)"))"
                    replacements[point.snapshotID] = QuotaHistoryPoint(
                        snapshotID: point.snapshotID, observedAt: point.observedAt,
                        day: point.day, remainingRatio: point.remainingRatio,
                        seriesID: point.seriesID, lineSegmentID: segmentID,
                        styleKey: point.styleKey, seriesLabel: point.seriesLabel,
                        resetsAt: point.resetsAt, state: point.state, stale: point.stale,
                        segmentStart: beginsGap ? .samplingGap : point.segmentStart
                    )
                }
            }
        }
        return points.map { replacements[$0.snapshotID] ?? $0 }
    }

    private static func resetChanged(from old: Date?, to new: Date?) -> Bool {
        switch (old, new) {
        case (nil, _), (_, nil): false
        case let (old?, new?): abs(new.timeIntervalSince(old)) > resetDriftTolerance
        }
    }

    private static func parseISO8601(_ value: String) -> Date? {
        if let date = try? Date(value, strategy: Date.ISO8601FormatStyle(includingFractionalSeconds: true)) {
            return date
        }
        return try? Date(value, strategy: .iso8601)
    }

    private static func pseudonymousAccount(_ value: String) -> String {
        "Account " + stableHash(value)
    }

    private static func stableHash(_ value: String) -> String {
        var hash: UInt64 = 14_695_981_039_346_656_037
        for byte in value.utf8 {
            hash ^= UInt64(byte)
            hash &*= 1_099_511_628_211
        }
        return String(String(format: "%08llx", hash).suffix(8))
    }
}

public enum UsageDetailsAggregator {
    public static func make(
        from dataset: ActivityDataset,
        heatmapRange: ClosedRange<LocalDay>? = nil,
        metricRange: ClosedRange<LocalDay>,
        providerIDs: Set<String> = [],
        modelIDs: Set<String> = [],
        staleProviderIDs: Set<String> = []
    ) -> UsageDetailsModel {
        let filteredRecords = dataset.records.filter {
            (providerIDs.isEmpty || providerIDs.contains($0.providerID))
                && (modelIDs.isEmpty || modelIDs.contains($0.modelID))
        }
        let selectedDataset = ActivityDataset(
            records: filteredRecords,
            coverage: dataset.coverage.filter { providerIDs.isEmpty || providerIDs.contains($0.providerID) },
            knownScopes: dataset.knownScopes.filter { providerIDs.isEmpty || providerIDs.contains($0.providerID) },
            revision: dataset.revision
        )
        let resolvedHeatmapRange = heatmapRange ?? selectedDataset.dateBounds ?? metricRange
        let heatmapDataset = filled(selectedDataset, over: resolvedHeatmapRange)
        let heatmap = ActivityAggregator.makeViewModel(from: heatmapDataset)

        let metricDataset = filled(ActivityDataset(
            records: filteredRecords.filter { metricRange.contains($0.day) },
            coverage: selectedDataset.coverage.filter { metricRange.contains($0.day) },
            knownScopes: selectedDataset.knownScopes,
            revision: dataset.revision
        ), over: metricRange)
        let metrics = ActivityAggregator.makeViewModel(from: metricDataset).metrics

        let chartRange = UsagePeriod.modelTrendRange(ending: metricRange.upperBound)
        let chartRecords = filteredRecords.filter { chartRange.contains($0.day) }
        let recordsByModel: [String: [DailyUsage]] = Dictionary(
            grouping: chartRecords, by: ModelSeriesIdentity.id
        )
        var rankedModels: [ModelComposition] = recordsByModel.map { modelID, records in
            let total = records.reduce(Int64(0)) { partial, record in partial + record.totalTokens }
            return ModelComposition(modelID: modelID, tokens: total)
        }
        rankedModels.sort { left, right in
            left.tokens == right.tokens ? left.modelID < right.modelID : left.tokens > right.tokens
        }
        let namedModels = Set(
            rankedModels.prefix(ModelSeriesIdentity.namedLimit).map { $0.modelID }
        )
        let modelRanks = Dictionary(uniqueKeysWithValues: rankedModels.enumerated().map { ($0.element.modelID, $0.offset) })
        let groupedByDay = Dictionary(grouping: chartRecords, by: \.day)
        let chartDataset = filled(ActivityDataset(
            records: chartRecords,
            coverage: selectedDataset.coverage.filter { chartRange.contains($0.day) },
            knownScopes: selectedDataset.knownScopes,
            revision: dataset.revision
        ), over: chartRange)
        let chartView = ActivityAggregator.makeViewModel(from: chartDataset)
        let chartDays = chartView.days.map { activityDay in
            let records = groupedByDay[activityDay.day] ?? []
            let grouped = Dictionary(grouping: records) { record in
                let modelID = ModelSeriesIdentity.id(for: record)
                return namedModels.contains(modelID) ? modelID : ModelSeriesIdentity.overflowID
            }
            var values: [ModelComposition] = grouped.map { modelID, rows in
                ModelComposition(
                    modelID: modelID,
                    tokens: rows.reduce(Int64(0)) { partial, row in partial + row.totalTokens }
                )
            }.filter { $0.tokens > 0 }
            values.sort { left, right in
                let leftRank = left.modelID == ModelSeriesIdentity.overflowID
                    ? Int.max : modelRanks[left.modelID] ?? Int.max - 1
                let rightRank = right.modelID == ModelSeriesIdentity.overflowID
                    ? Int.max : modelRanks[right.modelID] ?? Int.max - 1
                return leftRank == rightRank ? left.modelID < right.modelID : leftRank < rightRank
            }
            if activityDay.state == .coveredZero { values = [] }
            let quality = quality(for: activityDay, records: records)
            return DailyChartDay(
                day: activityDay.day, state: activityDay.state,
                totalTokens: activityDay.totalTokens, observedTokens: activityDay.observedTokens,
                composition: values, quality: quality
            )
        }
        let points: [DailyModelPoint] = chartDays.flatMap { day -> [DailyModelPoint] in
            guard day.state != ActivityDayState.missing, day.state != ActivityDayState.partial else {
                return []
            }
            return day.composition.map { DailyModelPoint(day: day.day, modelID: $0.modelID, tokens: $0.tokens) }
        }
        let recordsByDay = Dictionary(grouping: filteredRecords, by: \.day)
        let coverageByDay = Dictionary(grouping: selectedDataset.coverage, by: \.day)
        let heatmapDetails = heatmap.days.map { day in
            let rows = recordsByDay[day.day] ?? []
            let staleFromRows = rows.contains {
                staleProviderIDs.contains($0.providerID) || $0.quality.lowercased().contains("stale")
            }
            let staleFromCoverage = (coverageByDay[day.day] ?? []).contains {
                staleProviderIDs.contains($0.providerID)
            }
            return HeatmapDayDetail(
                activity: day,
                quality: quality(for: day, records: rows),
                lastCollectionAt: rows.map(\.importedAt).max(),
                isStale: staleFromRows || staleFromCoverage
            )
        }

        return UsageDetailsModel(
            heatmapDays: heatmap.days, heatmapDetails: heatmapDetails, metrics: metrics,
            seriesPoints: points, chartDays: chartDays,
            providerIDs: Set(dataset.records.map(\.providerID)).sorted(),
            modelIDs: Set(dataset.records.map(\.modelID)).sorted(),
            filterSignature: providerIDs.sorted().joined(separator: ",") + "|"
                + modelIDs.sorted().joined(separator: ",") + "|"
                + metricRange.lowerBound.rawValue + "|" + metricRange.upperBound.rawValue,
            revision: dataset.revision
        )
    }

    private static func filled(
        _ dataset: ActivityDataset, over range: ClosedRange<LocalDay>
    ) -> ActivityDataset {
        let records = dataset.records.filter { range.contains($0.day) }
        var coverage = dataset.coverage.filter { range.contains($0.day) }
        let scopes = Set(dataset.knownScopes)
            .union(records.map { ProviderScope(providerID: $0.providerID, accountRef: $0.accountRef) })
            .union(coverage.map { ProviderScope(providerID: $0.providerID, accountRef: $0.accountRef) })
        let existing = Set(coverage.map { "\($0.day.rawValue)|\($0.providerID)|\($0.accountRef)" })
        for day in range.days {
            for scope in scopes where !existing.contains("\(day.rawValue)|\(scope.providerID)|\(scope.accountRef)") {
                coverage.append(CoverageDay(
                    day: day, providerID: scope.providerID,
                    accountRef: scope.accountRef, isCovered: false
                ))
            }
        }
        if scopes.isEmpty, coverage.isEmpty {
            coverage = range.days.map {
                CoverageDay(day: $0, providerID: "__selection__", accountRef: "", isCovered: false)
            }
        }
        return ActivityDataset(
            records: records, coverage: coverage,
            knownScopes: scopes, revision: dataset.revision
        )
    }

    private static func quality(for day: ActivityDay, records: [DailyUsage]) -> ActivityQuality {
        switch day.state {
        case .missing: return .missing
        case .partial: return .partial
        case .coveredZero: return .exact
        case .coveredActive:
            return records.allSatisfy { ["exact", "direct", "live"].contains($0.quality.lowercased()) }
                ? .exact : .estimated
        }
    }
}

public enum HeatmapDirection: Sendable, Hashable { case up, down, left, right }

public enum HeatmapNavigation {
    public static func destination(from index: Int, direction: HeatmapDirection, count: Int) -> Int {
        guard count > 0, (0..<count).contains(index) else { return max(0, min(index, count - 1)) }
        let delta = switch direction {
        case .up: -1
        case .down: 1
        case .left: -7
        case .right: 7
        }
        let destination = index + delta
        return (0..<count).contains(destination) ? destination : index
    }
}

public enum ChartSelection {
    public static func index(at x: Double, plotWidth: Double, count: Int) -> Int? {
        guard count > 0, plotWidth > 0, x >= 0, x < plotWidth else { return nil }
        return min(count - 1, Int((x / plotWidth) * Double(count)))
    }
}

public enum ProviderProductCategory: String, Sendable, Hashable { case subscription, api, localTool }
public enum ProviderMetricFamily: String, Sendable, Hashable { case subscriptionQuota, tokenActivity, billing, operational }
public enum CredentialSourceType: String, Sendable, Hashable {
    case none, keychain, browserSession, apiKey, oauth, cli, local
}

public struct ProviderIdentitySource: Sendable, Hashable {
    public let credentialSource: String
    public let sourceKind: String

    public init(credentialSource: String, sourceKind: String) {
        self.credentialSource = credentialSource
        self.sourceKind = sourceKind
    }
}

public struct ProviderDisplayDescriptor: Sendable, Hashable {
    public let providerID: String
    public let familyID: String
    public let displayName: String
    public let category: ProviderProductCategory
    public let metricFamilies: Set<ProviderMetricFamily>
    public let regions: Set<String>
    public let supportsAccounts: Bool
    public let credentialSourceTypes: Set<CredentialSourceType>
    public let acceptedIdentitySources: Set<ProviderIdentitySource>
    public let capabilityProfile: ProviderCapabilityProfile
    public let sourceCapabilities: [ProviderSourceCapability]
}

public enum ProviderCatalog {
    public static var allDescriptors: [ProviderDisplayDescriptor] {
        GeneratedProviderCatalog.families.values.sorted {
            let order = $0.displayName.localizedStandardCompare($1.displayName)
            return order == .orderedSame ? $0.familyID < $1.familyID : order == .orderedAscending
        }
    }

    public static func descriptor(for providerID: String) -> ProviderDisplayDescriptor {
        if let descriptor = known[providerID] { return descriptor }
        return ProviderDisplayDescriptor(
            providerID: providerID, familyID: providerID,
            displayName: DisplayText.provider(providerID), category: .api,
            metricFamilies: [.tokenActivity, .billing], regions: [], supportsAccounts: false,
            credentialSourceTypes: [.none],
            acceptedIdentitySources: [], capabilityProfile: .unknown,
            sourceCapabilities: [.openUsageFallback]
        )
    }

    public static func descriptor(
        for providerID: String, familyID: String,
        displayName: String, category: ProviderProductCategory
    ) -> ProviderDisplayDescriptor {
        let knownFamily = known[familyID]
        let family = knownFamily ?? descriptor(for: familyID)
        let resolvedDisplayName: String
        if knownFamily != nil,
           displayName.unicodeScalars.allSatisfy({ $0.isASCII }),
           displayName.lowercased() == familyID.lowercased() {
            resolvedDisplayName = family.displayName
        } else {
            resolvedDisplayName = displayName.isEmpty ? family.displayName : displayName
        }
        return ProviderDisplayDescriptor(
            providerID: providerID,
            familyID: familyID,
            displayName: resolvedDisplayName,
            category: category,
            metricFamilies: family.metricFamilies,
            regions: family.regions,
            supportsAccounts: family.supportsAccounts,
            credentialSourceTypes: family.credentialSourceTypes,
            acceptedIdentitySources: family.acceptedIdentitySources,
            capabilityProfile: family.capabilityProfile,
            sourceCapabilities: family.sourceCapabilities
        )
    }

    private static let known = GeneratedProviderCatalog.families
}

public enum UsageDetailsRoute: String, CaseIterable, Sendable, Hashable, Identifiable {
    case activity, capacity, apiSpend, localTools, providersAndAccounts, dataHealth
    public var id: String { rawValue }

    public init(arguments: [String]) {
        guard let index = arguments.firstIndex(of: "--route"), arguments.indices.contains(index + 1) else {
            self = .activity
            return
        }
        self = Self(routeValue: arguments[index + 1]) ?? .activity
    }

    public init?(routeValue: String) {
        switch routeValue.lowercased() {
        case "overview", "welcome", "activity": self = .activity
        case "capacity": self = .capacity
        case "api-spend", "spend": self = .apiSpend
        case "local-tools", "local": self = .localTools
        case "providers", "accounts", "settings": self = .providersAndAccounts
        case "health", "data-health": self = .dataHealth
        default: return nil
        }
    }
}

public enum ActivityRouteMessage {
    public static let notificationName = Notification.Name("com.openusage.bar.activity.route-request")
    public static let routeKey = "route"

    public static func userInfo(for route: UsageDetailsRoute) -> [String: String] {
        [routeKey: route.transportValue]
    }

    public static func decode(_ userInfo: [String: String]) -> UsageDetailsRoute? {
        guard userInfo.count == 1, let raw = userInfo[routeKey] else { return nil }
        return UsageDetailsRoute(routeValue: raw)
    }
}

public extension UsageDetailsRoute {
    var transportValue: String {
        switch self {
        case .apiSpend: "api-spend"
        case .localTools: "local-tools"
        case .providersAndAccounts: "providers"
        case .dataHealth: "health"
        default: rawValue
        }
    }
}

private extension ActivityDataset {
    var dateBounds: ClosedRange<LocalDay>? {
        let days = Set(records.map(\.day)).union(coverage.map(\.day)).sorted()
        guard let first = days.first, let last = days.last else { return nil }
        return first...last
    }
}

public enum TokenText {
    public static func compact(_ tokens: Int64) -> String {
        let value = Double(tokens)
        if abs(value) >= 1_000_000_000 { return number(value / 1_000_000_000) + "B" }
        if abs(value) >= 1_000_000 { return number(value / 1_000_000) + "M" }
        if abs(value) >= 1_000 { return number(value / 1_000) + "K" }
        return String(tokens)
    }

    private static func number(_ value: Double) -> String {
        value >= 100 || value.rounded() == value
            ? String(format: "%.0f", value) : String(format: "%.1f", value)
    }
}

public enum DisplayText {
    public static func provider(_ id: String) -> String {
        if let descriptor = GeneratedProviderCatalog.families[id] {
            return descriptor.displayName
        }
        return id.replacingOccurrences(of: "_", with: " ")
            .replacingOccurrences(of: "-", with: " ").capitalized
    }

    public static func model(_ id: String) -> String {
        if id == ModelSeriesIdentity.overflowID { return "Additional Models" }
        if id == "unknown" { return "Unattributed" }
        if let providerID = ModelSeriesIdentity.unattributedProviderID(from: id) {
            return "\(provider(providerID)) · Unattributed"
        }
        return id.split(separator: "-").map { part in
            let value = String(part)
            return value.allSatisfy(\.isNumber) ? value : value.uppercased()
        }.joined(separator: "-")
    }
}

public extension ActivityQuality {
    var displayName: String {
        switch self {
        case .exact: "Exact"
        case .estimated: "Estimated"
        case .partial: "Partial"
        case .missing: "Missing"
        }
    }
}
