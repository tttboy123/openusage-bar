import Foundation
import Observation
import UsageCore

enum ActivitySelectionMatch: Sendable, Hashable {
    case matched
    case noMatchingProvider(String)
    case noMatchingModel(String)
}

struct ActivityLoadedData: Sendable, Hashable {
    let details: UsageDetailsModel
    let capacity: [CapacityItem]
    let quotaHistory: [QuotaHistoryPoint]
    let quotaHistoryIsPartial: Bool
    let health: SourceHealth
    let records: [DailyUsage]
    let historyRecords: [DailyUsage]
    let hiddenProviderIDs: Set<String>
    let visibilityIssue: Bool
    let latestCollectionAt: String?
    let availableProviderIDs: [String]
    let providerDescriptors: [String: ProviderDisplayDescriptor]
    let availableModelIDs: [String]
    let revision: Int64
    let selectionMatch: ActivitySelectionMatch
    let requestSignature: String
    let apiSpend: APISpendSummary

    var visibleProviderIDs: [String] {
        availableProviderIDs
    }

    func providerDescriptor(for providerID: String) -> ProviderDisplayDescriptor {
        providerDescriptors[providerID] ?? ProviderCatalog.descriptor(for: providerID)
    }
}

protocol ActivityLoading: Sendable {
    func load(_ request: ActivityLoadRequest) async throws -> ActivityLoadedData
}

enum ActivityLoadPhase: Sendable, Hashable {
    case beforeQuotaHistory
    case afterQuotaHistory
}

actor ActivityDataLoader: ActivityLoading {
    private let databaseURL: URL
    private let visibilityURL: URL
    private let calendar: Calendar
    private let loadHook: @Sendable (ActivityLoadPhase, Int) throws -> Void

    init(
        databaseURL: URL = ActivityPaths.ledger,
        visibilityURL: URL = ActivityPaths.visibility,
        calendar: Calendar = .current,
        timeZone: TimeZone = .current,
        loadHook: @escaping @Sendable (ActivityLoadPhase, Int) throws -> Void = { _, _ in }
    ) {
        self.databaseURL = databaseURL
        self.visibilityURL = visibilityURL
        var configuredCalendar = calendar
        configuredCalendar.timeZone = timeZone
        self.calendar = configuredCalendar
        self.loadHook = loadHook
    }

    func load(_ request: ActivityLoadRequest) throws -> ActivityLoadedData {
        try Task.checkCancellation()
        let visibility: VisibilitySnapshot
        let visibilityIssue: Bool
        do {
            visibility = try VisibilityStore(url: visibilityURL).load()
            visibilityIssue = false
        } catch {
            visibility = VisibilitySnapshot(hiddenProviderIDs: [], revision: 0)
            visibilityIssue = true
        }

        let repository = try UsageRepository(databaseURL: databaseURL)
        defer { repository.close() }
        for attempt in 0..<2 {
            try Task.checkCancellation()
            let startRevision = try repository.dataRevision()
            let dataset = try repository.activity(
                from: request.repositoryRange.lowerBound,
                to: request.repositoryRange.upperBound
            )
            let costDataset = try repository.dailyCosts(
                from: request.repositoryRange.lowerBound,
                to: request.repositoryRange.upperBound
            )
            let capacity = try repository.capacity(limit: nil)
            let health = try repository.sourceHealth()
            let instances = try repository.providerInstances()
            let instanceDescriptors = Dictionary(
                uniqueKeysWithValues: instances.map { ($0.providerID, $0.descriptor) }
            )
            let providerHealthSources = health.sources.filter {
                !OpenUsageCatalogPresentation.isCatalogSource($0)
            }
            let knownIDs = Set(dataset.records.map(\.providerID))
                .union(dataset.knownScopes.map(\.providerID))
                .union(capacity.map(\.providerID))
                .union(providerHealthSources.map(\.providerID))
                .union(instances.map(\.providerID))
                .union(costDataset.records.map(\.providerID))
                .union(costDataset.knownScopes.map(\.providerID))
            let providerDescriptors = Dictionary(uniqueKeysWithValues: knownIDs.map { providerID in
                (providerID, instanceDescriptors[providerID] ?? ProviderCatalog.descriptor(for: providerID))
            })
            let visibleIDs = knownIDs.subtracting(visibility.hiddenProviderIDs)
            let visibleDataset = ActivityDataset(
                records: dataset.records.filter { visibleIDs.contains($0.providerID) },
                coverage: dataset.coverage.filter { visibleIDs.contains($0.providerID) },
                knownScopes: dataset.knownScopes.filter { visibleIDs.contains($0.providerID) },
                revision: dataset.revision
            )
            let effectiveProviders = request.providerIDs.intersection(visibleIDs)
            let selectedCostScopes = Set(costDataset.knownScopes.filter {
                visibleIDs.contains($0.providerID)
                    && (request.providerIDs.isEmpty || effectiveProviders.contains($0.providerID))
            })
            let selectedCosts = DailyCostDataset(
                records: costDataset.records.filter {
                    visibleIDs.contains($0.providerID)
                        && (request.providerIDs.isEmpty || effectiveProviders.contains($0.providerID))
                },
                coverage: costDataset.coverage.filter {
                    visibleIDs.contains($0.providerID)
                        && (request.providerIDs.isEmpty || effectiveProviders.contains($0.providerID))
                },
                knownScopes: selectedCostScopes,
                revision: costDataset.revision
            )
            let providerScopedDataset = ActivityDataset(
                records: visibleDataset.records.filter {
                    request.providerIDs.isEmpty || effectiveProviders.contains($0.providerID)
                },
                coverage: visibleDataset.coverage.filter {
                    request.providerIDs.isEmpty || effectiveProviders.contains($0.providerID)
                },
                knownScopes: visibleDataset.knownScopes.filter {
                    request.providerIDs.isEmpty || effectiveProviders.contains($0.providerID)
                },
                revision: dataset.revision
            )
            let availableModelsForProvider = Set(providerScopedDataset.records.map(\.modelID))
            let effectiveModels = request.modelIDs.intersection(availableModelsForProvider)
            let selectionMatch: ActivitySelectionMatch
            if let missing = request.providerIDs.subtracting(visibleIDs).sorted().first {
                selectionMatch = .noMatchingProvider(missing)
            } else if let missing = request.modelIDs.subtracting(availableModelsForProvider).sorted().first {
                selectionMatch = .noMatchingModel(missing)
            } else {
                selectionMatch = .matched
            }
            let filteredRecords: [DailyUsage]
            let historyRecords: [DailyUsage]
            let details: UsageDetailsModel
            let hasTokenScope = !providerScopedDataset.records.isEmpty
                || !providerScopedDataset.coverage.isEmpty
                || !providerScopedDataset.knownScopes.isEmpty
            if selectionMatch == .matched, hasTokenScope {
                filteredRecords = providerScopedDataset.records.filter {
                    request.modelIDs.isEmpty || effectiveModels.contains($0.modelID)
                }.filter { request.metricRange.contains($0.day) }
                historyRecords = providerScopedDataset.records.filter {
                    request.modelIDs.isEmpty || effectiveModels.contains($0.modelID)
                }
                let staleProviders = Self.tokenHistoryStaleProviderIDs(health.sources)
                details = UsageDetailsAggregator.make(
                    from: providerScopedDataset,
                    heatmapRange: request.repositoryRange,
                    metricRange: request.metricRange,
                    modelIDs: effectiveModels,
                    staleProviderIDs: staleProviders
                )
            } else {
                filteredRecords = []
                historyRecords = []
                details = .empty(filterSignature: request.signature, revision: dataset.revision)
            }
            let providerPredicate: (String) -> Bool = { providerID in
                request.providerIDs.isEmpty || effectiveProviders.contains(providerID)
            }
            let capacityRows = capacity.filter {
                selectionMatch.providerMatchesQuota && visibleIDs.contains($0.providerID)
                    && providerPredicate($0.providerID)
            }
            let quotaProviderIDs: Set<String> = selectionMatch.providerMatchesQuota
                ? (request.providerIDs.isEmpty ? visibleIDs : effectiveProviders)
                : []
            let quotaBounds = try Self.quotaBounds(for: request.metricRange, calendar: calendar)
            try Task.checkCancellation()
            try loadHook(.beforeQuotaHistory, attempt)
            let quotaResult = try repository.quotaHistory(
                providerIDs: quotaProviderIDs,
                observedAtOrAfter: quotaBounds.start,
                observedBefore: quotaBounds.endExclusive,
                perSeriesLimit: 1_000
            )
            try loadHook(.afterQuotaHistory, attempt)
            let finalRevision = try repository.dataRevision()
            guard startRevision == finalRevision,
                  dataset.revision == finalRevision,
                  costDataset.revision == finalRevision,
                  health.revision == finalRevision
            else {
                if attempt == 0 { continue }
                throw RepositoryError.corruptData
            }
            let historyRows = try QuotaHistoryPresentation.make(
                items: quotaResult.items, calendar: calendar,
                providerDescriptors: providerDescriptors
            )
            let selectedHealth = health.sources.filter {
                OpenUsageCatalogPresentation.isCatalogSource($0)
                    || (selectionMatch == .matched && visibleIDs.contains($0.providerID)
                        && providerPredicate($0.providerID))
            }
            let latest = Self.latestTimestamp(
                filteredRecords.map(\.importedAt)
                    + selectedCosts.records.map(\.importedAt)
                    + selectedHealth.filter {
                        !OpenUsageCatalogPresentation.isCatalogSource($0)
                    }.compactMap(\.lastSuccessAt)
            )
            return ActivityLoadedData(
                details: details,
                capacity: capacityRows,
                quotaHistory: historyRows,
                quotaHistoryIsPartial: quotaResult.isTruncated,
                health: SourceHealth(
                    sources: selectedHealth,
                    hasIssues: OpenUsageCatalogPresentation.globalHealthHasIssues(
                        selectedHealth
                    ) || visibilityIssue,
                    revision: health.revision
                ),
                records: filteredRecords,
                historyRecords: historyRecords,
                hiddenProviderIDs: visibility.hiddenProviderIDs,
                visibilityIssue: visibilityIssue,
                latestCollectionAt: latest,
                availableProviderIDs: visibleIDs.sorted(),
                providerDescriptors: providerDescriptors,
                availableModelIDs: Set(visibleDataset.records.map(\.modelID)).sorted(),
                revision: finalRevision,
                selectionMatch: selectionMatch,
                requestSignature: request.signature,
                apiSpend: APISpendAggregator.make(
                    costs: selectedCosts,
                    legacyRecords: filteredRecords,
                    range: request.metricRange,
                    isLegacyCoverageComplete: details.metrics.isComplete
                )
            )
        }
        throw RepositoryError.corruptData
    }

    static func tokenHistoryStaleProviderIDs(_ sources: [SourceHealthItem]) -> Set<String> {
        Set(sources.lazy.filter {
            $0.sourceID == "openusage.daily" && $0.effectiveState.lowercased() == "stale"
        }.map(\.providerID))
    }

    static func quotaBounds(
        for range: ClosedRange<LocalDay>, calendar: Calendar
    ) throws -> (start: String, endExclusive: String) {
        func date(_ day: LocalDay) throws -> Date {
            let values = day.rawValue.split(separator: "-").compactMap { Int($0) }
            guard values.count == 3,
                  let date = calendar.date(from: DateComponents(
                    timeZone: calendar.timeZone, year: values[0], month: values[1], day: values[2]
                  ))
            else { throw RepositoryError.invalidRequest }
            return date
        }
        let start = try date(range.lowerBound)
        let finalDay = try date(range.upperBound)
        guard let end = calendar.date(byAdding: .day, value: 1, to: finalDay) else {
            throw RepositoryError.invalidRequest
        }
        let formatter = ISO8601DateFormatter()
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        return (formatter.string(from: start), formatter.string(from: end))
    }

    static func latestTimestamp(_ values: [String]) -> String? {
        values.compactMap { value -> (String, Date)? in
            ActivityTimestamp.date(from: value).map { (value, $0) }
        }.max { $0.1 < $1.1 }?.0
    }
}

enum ActivityTimestamp {
    private static let grammar = #"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"#

    static func date(from value: String) -> Date? {
        guard value.range(of: grammar, options: .regularExpression) != nil else { return nil }
        return try? Date(value, strategy: Date.ISO8601FormatStyle(includingFractionalSeconds: true))
    }
}

private extension ActivitySelectionMatch {
    var providerMatchesQuota: Bool {
        if case .noMatchingProvider = self { return false }
        return true
    }
}

@MainActor
@Observable
final class ActivityViewStore {
    private(set) var data: ActivityLoadedData?
    private(set) var error: RepositoryError?
    private(set) var isLoading = false
    private(set) var selectionRevalidationPending = false
    var period: UsagePeriod = .year
    var providerID: String?
    var modelID: String?
    var chartFocus = ChartFocus(day: nil)
    var modelSeriesVisibility = ModelSeriesVisibility()
    var heatmapFocusDay: LocalDay?

    @ObservationIgnored private let loader: any ActivityLoading
    @ObservationIgnored private let preferences: ActivityPreferences
    @ObservationIgnored private var task: Task<Void, Never>?
    @ObservationIgnored private var gate = ActivityLoadGate()

    init(
        loader: any ActivityLoading = ActivityDataLoader(),
        preferences: ActivityPreferences = ActivityPreferences()
    ) {
        self.loader = loader
        self.preferences = preferences
        let saved = preferences.load()
        period = saved.period
        providerID = saved.providerID
        modelID = saved.modelID
    }

    var providers: [String] { data?.visibleProviderIDs ?? [] }
    var models: [String] { data?.availableModelIDs ?? [] }
    var displayData: ActivityLoadedData? {
        guard !selectionRevalidationPending,
              data?.requestSignature == currentRequest.signature
        else { return nil }
        return data
    }

    func revalidateSelection() {
        clearFocus()
        selectionRevalidationPending = data != nil
        reload()
    }

    func updateFilters(period: UsagePeriod, providerID: String?, modelID: String?) {
        clearFocus()
        self.period = period
        self.providerID = providerID
        self.modelID = modelID
        preferences.save(.init(period: period, providerID: providerID, modelID: modelID))
        reload()
    }

    func filtersDidChange() {
        updateFilters(period: period, providerID: providerID, modelID: modelID)
    }

    @discardableResult
    func toggleModelSeries(_ seriesID: String, availableSeriesIDs: [String]) -> Bool {
        guard modelSeriesVisibility.toggle(
            seriesID, availableSeriesIDs: availableSeriesIDs
        ) else { return false }
        chartFocus.clear(.filterChange)
        return true
    }

    func reconcileModelSeries(availableSeriesIDs: [String]) {
        let previous = modelSeriesVisibility
        modelSeriesVisibility.reconcile(availableSeriesIDs: availableSeriesIDs)
        if previous != modelSeriesVisibility {
            chartFocus.clear(.filterChange)
        }
    }

    func showAllModelSeries() {
        guard !modelSeriesVisibility.hiddenSeriesIDs.isEmpty else { return }
        modelSeriesVisibility.showAll()
        chartFocus.clear(.filterChange)
    }

    func clearFilters() {
        preferences.clearFilters()
        updateFilters(period: period, providerID: nil, modelID: nil)
    }

    func reload() {
        task?.cancel()
        let generation = gate.begin()
        let request = currentRequest
        isLoading = true
        task = Task {
            do {
                let loaded = try await loader.load(request)
                guard !Task.isCancelled, gate.canPublish(generation) else { return }
                reconcileModelSeries(availableSeriesIDs: loaded.details.modelSeriesIDs)
                data = loaded
                error = nil
                isLoading = false
                selectionRevalidationPending = false
            } catch is CancellationError {
                return
            } catch let repositoryError as RepositoryError {
                guard !Task.isCancelled, gate.canPublish(generation) else { return }
                error = repositoryError
                isLoading = false
            } catch {
                guard !Task.isCancelled, gate.canPublish(generation) else { return }
                self.error = .corruptData
                isLoading = false
            }
        }
    }

    func cancel() {
        clearFocus()
        task?.cancel()
        gate.cancel()
        isLoading = false
    }

    private var currentRequest: ActivityLoadRequest {
        ActivityLoadRequest(
            period: period, ending: Self.today(),
            providerIDs: providerID.map { [$0] } ?? [],
            modelIDs: modelID.map { [$0] } ?? []
        )
    }

    private func clearFocus() {
        chartFocus.clear(.filterChange)
        heatmapFocusDay = nil
    }

    private static func today() -> LocalDay {
        let parts = Calendar.current.dateComponents([.year, .month, .day], from: Date())
        return try! LocalDay(String(format: "%04d-%02d-%02d", parts.year!, parts.month!, parts.day!))
    }
}
