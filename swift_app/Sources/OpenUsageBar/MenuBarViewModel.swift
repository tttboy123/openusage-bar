import AppKit
import Foundation
import Observation
import UsageCore

@MainActor
@Observable
final class MenuBarViewModel {
    private(set) var summary: CompactSummary?
    private(set) var loadError: String?
    private(set) var refreshError: String?
    private(set) var visibilityError: String?
    private(set) var isRefreshing = false
    var expandedProviderID: String?
    var selectedProviderID: String?

    private var repository: UsageRepository?
    private let ledgerURL: URL
    private let visibilityStore: VisibilityStore
    private var hiddenProviderIDs = Set<String>()
    private var visibilityRevision: UInt64?
    private var loadGate = LoadGate()
    private var freshnessTimer: Timer?

    init(
        ledgerURL: URL = InstalledPaths.ledger,
        visibilityURL: URL = InstalledPaths.visibility
    ) {
        self.ledgerURL = ledgerURL
        visibilityStore = VisibilityStore(url: visibilityURL)
    }

    var groups: [ProviderCapacityGroup] {
        ProviderCapacityGroup.make(from: (summary?.capacity ?? []).filter {
            !hiddenProviderIDs.contains($0.providerID)
        })
    }
    var statusLabel: StatusLabel { .compact(remainingRatio: groups.first?.primary.remainingRatio) }
    var accessibilityTitle: String { statusLabel.accessibilityTitle }
    var accessibilityValue: String { statusLabel.accessibilityValue }
    var hasHealthIssues: Bool {
        summary?.hasHealthIssues == true || loadError != nil || refreshError != nil || visibilityError != nil
    }
    var displayError: String? { loadError ?? refreshError ?? visibilityError }
    var emptyStatePresentation: MenuEmptyStatePresentation {
        MenuEmptyStatePresentation.make(
            hasSources: summary.map {
                $0.todayTokens != nil || !$0.capacity.isEmpty || $0.updatedAt != nil
            } ?? false,
            isRefreshing: isRefreshing,
            hasFailure: displayError != nil
        )
    }
    var isMonitoring: Bool { freshnessTimer?.isValid == true }
    var todayPresentation: TodayTokenPresentation {
        TodayTokenPresentation(
            tokens: summary?.todayTokens,
            isComplete: summary?.isTodayComplete ?? false
        )
    }
    var updatedAge: String {
        guard let value = summary?.updatedAt, let date = Format.timestamp(value) else {
            return "Waiting for usage data"
        }
        return "Updated \(Format.age(Int64(max(0, Date().timeIntervalSince(date))))) ago"
    }

    func loadLastGoodOnce() {
        guard loadGate.beginInitialLoad() else { return }
        reloadVisibility()
        loadFromLedger()
    }

    func startMonitoring(interval: TimeInterval = 10) {
        loadLastGoodOnce()
        guard freshnessTimer == nil, interval > 0 else { return }
        let timer = Timer(timeInterval: interval, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.checkFreshness() }
        }
        freshnessTimer = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    func stopMonitoring() {
        freshnessTimer?.invalidate()
        freshnessTimer = nil
    }

    private func loadFromLedger() {
        do {
            let next = try UsageRepository(databaseURL: ledgerURL)
            repository?.close()
            repository = next
            let loaded = try next.compactSummary(on: Self.localToday())
            summary = loaded
            loadGate.finish(revision: loaded.revision)
            loadError = nil
            if selectedProviderID == nil { selectedProviderID = groups.first?.id }
        } catch {
            loadError = error.localizedDescription
            loadGate.finish(revision: nil)
        }
    }

    func checkFreshness() {
        reloadVisibility()
        do {
            let reader = try repository ?? UsageRepository(databaseURL: ledgerURL)
            let observed = try reader.dataRevision()
            if loadGate.shouldReload(observed: observed) { loadFromLedger() }
            else if repository == nil { reader.close() }
        } catch {
            loadError = error.localizedDescription
        }
    }

    func refresh() {
        reloadVisibility()
        guard !isRefreshing else { return }
        guard let command = InstalledPaths.refreshCommand() else {
            refreshError = "Installed collector is unavailable."
            return
        }
        isRefreshing = true
        Task {
            let result = await Task.detached { RefreshRunner().run(command) }.value
            isRefreshing = false
            refreshError = RefreshErrorPolicy.next(current: refreshError, result: result)
            if result == .succeeded {
                checkFreshness()
            }
        }
    }

    private func reloadVisibility() {
        do {
            let snapshot = try visibilityStore.load()
            if snapshot.revision != visibilityRevision {
                hiddenProviderIDs = snapshot.hiddenProviderIDs
                visibilityRevision = snapshot.revision
            }
            visibilityError = nil
        } catch {
            hiddenProviderIDs = []
            visibilityRevision = nil
            visibilityError = "Provider visibility settings are invalid."
        }
        let visibleIDs = Set(groups.map(\.id))
        if let selectedProviderID, !visibleIDs.contains(selectedProviderID) {
            self.selectedProviderID = groups.first?.id
        }
        if let expandedProviderID, !visibleIDs.contains(expandedProviderID) {
            self.expandedProviderID = nil
        }
    }

    func toggle(_ id: String) {
        let hasSecondary = groups.first(where: { $0.id == id })?.secondary.isEmpty == false
        expandedProviderID = ExpansionPolicy.next(
            current: expandedProviderID, selected: id, hasSecondary: hasSecondary
        )
    }

    func moveSelection(_ offset: Int) {
        let ids = groups.map(\.id)
        guard !ids.isEmpty else { return }
        let current = selectedProviderID.flatMap(ids.firstIndex) ?? (offset > 0 ? -1 : 0)
        selectedProviderID = ids[(current + offset + ids.count) % ids.count]
    }

    func activateSelection() {
        if let selectedProviderID { toggle(selectedProviderID) }
    }

    func perform(_ action: MenuAction) {
        switch action {
        case .refresh: refresh()
        case .openDetails: HelperLauncher.openActivity()
        case .openSettings: HelperLauncher.openSettings()
        case .collapse: expandedProviderID = nil
        case .close: NSApp.keyWindow?.close()
        case let .moveSelection(offset): moveSelection(offset)
        case .activateSelection: activateSelection()
        }
    }

    private static func localToday() -> LocalDay {
        let parts = Calendar.current.dateComponents([.year, .month, .day], from: Date())
        return try! LocalDay(String(format: "%04d-%02d-%02d", parts.year!, parts.month!, parts.day!))
    }
}
