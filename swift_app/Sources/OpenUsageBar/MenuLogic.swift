import Foundation
import UsageCore

public struct StatusLabel: Sendable, Hashable {
    public let values: [String]
    public let accessibilityTitle: String
    public let accessibilityValue: String

    public var text: String? { values.isEmpty ? nil : values.joined(separator: " ") }

    public static func compact(remainingRatio: Double?) -> Self {
        let values = remainingRatio.map { [Format.percent($0)] } ?? []
        return Self(
            values: values, accessibilityTitle: "OpenUsage Bar",
            accessibilityValue: values.first.map {
                AppLocalization.format("Most urgent capacity, %@ remaining", $0)
            } ?? AppLocalization.text("Capacity unavailable")
        )
    }

    public static func activity(tokens: Int64) -> Self {
        let value = Format.tokens(tokens)
        return Self(
            values: [value], accessibilityTitle: "OpenUsage Bar",
            accessibilityValue: AppLocalization.format("Today Token, %@", value)
        )
    }

    public static func custom(values: [String]) -> Self {
        let short = Array(values.filter { !$0.isEmpty }.prefix(2))
        return Self(
            values: short, accessibilityTitle: "OpenUsage Bar",
            accessibilityValue: short.isEmpty
                ? AppLocalization.text("Usage unavailable") : short.joined(separator: ", ")
        )
    }
}

public enum MenuCopy {
    public static let topLevelSections = ["Today Token", "Capacity"]
    public static let footerActions = ["Open Usage Details", "Data Health", "Settings"]
    public static let allVisibleText = [
        "OpenUsage Bar", "Updated", "Refresh", "Today Token", "Capacity",
        "Most urgent first", "View all providers", "Open Usage Details", "Data Health", "Settings",
    ]
}

enum MenuDestination {
    static let allProviders = UsageDetailsRoute.providersAndAccounts
}

public enum MenuKey: Sendable, Hashable {
    case commandRefresh, commandDetails, commandSettings
    case escape, up, down, returnKey, space
}

public enum MenuAction: Sendable, Hashable {
    case refresh, openDetails, openSettings, collapse, close
    case moveSelection(Int), activateSelection
}

public enum MenuKeyRouter {
    public static func action(for key: MenuKey, hasExpansion: Bool = false) -> MenuAction {
        switch key {
        case .commandRefresh: .refresh
        case .commandDetails: .openDetails
        case .commandSettings: .openSettings
        case .escape: hasExpansion ? .collapse : .close
        case .up: .moveSelection(-1)
        case .down: .moveSelection(1)
        case .returnKey, .space: .activateSelection
        }
    }
}

public enum RevisionGate {
    public static func shouldReload(current: Int64?, observed: Int64) -> Bool { current != observed }
}

struct LoadGate: Sendable, Hashable {
    private enum Phase: Sendable, Hashable { case never, loading, loaded }
    private var phase = Phase.never
    private(set) var revision: Int64?

    mutating func beginInitialLoad() -> Bool {
        guard phase == .never else { return false }
        phase = .loading
        return true
    }

    mutating func finish(revision: Int64?) {
        self.revision = revision
        phase = .loaded
    }

    func shouldReload(observed: Int64) -> Bool {
        phase == .loaded && RevisionGate.shouldReload(current: revision, observed: observed)
    }
}

enum ExpansionPolicy {
    static func next(current: String?, selected: String, hasSecondary: Bool) -> String? {
        guard hasSecondary else { return nil }
        return current == selected ? nil : selected
    }
}

enum RefreshErrorPolicy {
    static func next(current: String?, result: RefreshResult) -> String? {
        switch result {
        case .succeeded: nil
        case .failed: AppLocalization.text("Refresh failed. Showing last-good data.")
        case .timedOut: AppLocalization.text("Refresh timed out. Showing last-good data.")
        }
    }
}

public enum ProviderRiskLevel: Sendable, Hashable {
    case normal
    case warning
    case critical
}

struct TodayTokenPresentation: Sendable, Hashable {
    let value: String
    let coverage: String?
    let accessibilityValue: String

    init(tokens: Int64?, isComplete: Bool) {
        value = Format.todayTokens(tokens)
        coverage = tokens != nil && !isComplete ? AppLocalization.text("Partial") : nil
        accessibilityValue = [value, coverage].compactMap { $0 }.joined(separator: ", ")
    }
}

struct MenuEmptyStatePresentation: Sendable, Hashable {
    enum Reason: Sendable, Hashable {
        case noSources
        case collecting
        case sourceFailure
    }

    let reason: Reason
    let titleKey: String
    let detailKey: String
    let actionKey: String
    let primaryRoute: UsageDetailsRoute

    static func make(
        hasSources: Bool, isRefreshing: Bool, hasFailure: Bool
    ) -> Self {
        if hasFailure {
            return Self(
                reason: .sourceFailure,
                titleKey: "Last-good data unavailable",
                detailKey: "Open Data Health to inspect the source failure.",
                actionKey: "Open Data Health",
                primaryRoute: .dataHealth
            )
        }
        if isRefreshing || hasSources {
            return Self(
                reason: .collecting,
                titleKey: "Collecting usage data",
                detailKey: "Refresh is in progress. Existing facts remain available.",
                actionKey: "View Activity",
                primaryRoute: .activity
            )
        }
        return Self(
            reason: .noSources,
            titleKey: "No capacity data",
            detailKey: "Connect a supported provider in Settings.",
            actionKey: "Review Providers",
            primaryRoute: .providersAndAccounts
        )
    }
}

enum MenuCapacityOrdering {
    static func sorted(_ rows: [CapacityItem]) -> [CapacityItem] {
        rows.sorted(by: isBefore)
    }

    static func isBefore(_ lhs: CapacityItem, _ rhs: CapacityItem) -> Bool {
        let leftState = usabilityRank(lhs)
        let rightState = usabilityRank(rhs)
        if leftState != rightState { return leftState < rightState }
        switch (lhs.remainingRatio, rhs.remainingRatio) {
        case let (left?, right?) where left != right: return left < right
        case (_?, nil): return true
        case (nil, _?): return false
        default: break
        }
        switch (lhs.resetsAt.flatMap(Format.timestamp), rhs.resetsAt.flatMap(Format.timestamp)) {
        case let (left?, right?) where left != right: return left < right
        case (_?, nil): return true
        case (nil, _?): return false
        default: break
        }
        if lhs.providerID != rhs.providerID { return lhs.providerID < rhs.providerID }
        if lhs.accountRef != rhs.accountRef { return lhs.accountRef < rhs.accountRef }
        if lhs.quotaName != rhs.quotaName { return lhs.quotaName < rhs.quotaName }
        return lhs.recordID < rhs.recordID
    }

    private static func usabilityRank(_ row: CapacityItem) -> Int {
        let hasValue = row.remainingRatio != nil || row.remaining != nil
        if hasValue, row.state.lowercased() == "ok", !row.stale { return 0 }
        if hasValue { return 1 }
        return row.state.lowercased() == "unknown" ? 3 : 2
    }
}

public struct ProviderRowPresentation: Sendable, Hashable {
    public let providerDescriptor: ProviderDisplayDescriptor
    public let provider: String
    public let window: String
    public let capacity: String
    public let reset: String
    public let freshness: String?
    public let visibleMetadata: [String]
    public let qualityText: String
    public let riskText: String
    public let riskLevel: ProviderRiskLevel
    public let stateSymbol: String?
    public let accessibilityLabel: String
    public let accessibilityValue: String
    public let isCritical: Bool
    public let isWarning: Bool

    public init(_ row: CapacityItem, now: Date = Date()) {
        providerDescriptor = row.providerDescriptor
        provider = providerDescriptor.displayName
        window = Format.window(row.quotaName)
        if let ratio = row.remainingRatio {
            capacity = Format.percent(ratio)
        } else if let remaining = row.remaining {
            capacity = remaining + (row.unit == "count" ? "" : " \(row.unit)")
        } else {
            capacity = AppLocalization.text("Unavailable")
        }
        reset = Format.reset(row.resetsAt, now: now)
        if row.stale {
            freshness = AppLocalization.format(
                "Stale, %@ old", Format.age(row.freshnessSeconds)
            )
            stateSymbol = "clock.badge.exclamationmark"
        } else if row.state.lowercased() == "unknown" {
            freshness = AppLocalization.text("Unknown")
            stateSymbol = "questionmark.circle"
        } else if row.state != "ok" {
            freshness = Format.state(row.state)
            stateSymbol = "exclamationmark.triangle.fill"
        } else if row.remainingRatio == nil, row.remaining == nil {
            freshness = AppLocalization.text("Unavailable")
            stateSymbol = "xmark.circle"
        } else {
            freshness = nil
            stateSymbol = row.remainingRatio.map { $0 <= 0.2 ? "exclamationmark.triangle.fill" : nil } ?? nil
        }
        if row.remainingRatio.map({ $0 <= 0.2 }) == true {
            riskLevel = .critical
        } else if row.stale || row.state != "ok" || row.remainingRatio.map({ $0 <= 0.4 }) == true {
            riskLevel = .warning
        } else {
            riskLevel = .normal
        }
        isCritical = riskLevel == .critical
        isWarning = riskLevel == .warning
        qualityText = Format.quality(row.quality)
        switch riskLevel {
        case .critical: riskText = AppLocalization.text("Critical capacity")
        case .warning: riskText = AppLocalization.text("Warning capacity")
        case .normal: riskText = AppLocalization.text("Capacity normal")
        }
        visibleMetadata = [reset, freshness].compactMap { $0 }.filter { !$0.isEmpty }
        accessibilityLabel = "\(provider), \(window)"
        accessibilityValue = [
            window, AppLocalization.format("%@ remaining", capacity),
            reset, freshness, qualityText, riskText,
        ]
            .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: ", ")
    }
}

public struct ProviderCapacityGroup: Sendable, Hashable, Identifiable {
    public let id: String
    public let rows: [CapacityItem]
    public var primary: CapacityItem { rows[0] }
    public var secondary: ArraySlice<CapacityItem> { rows.dropFirst() }

    public static func make(from rows: [CapacityItem]) -> [Self] {
        let sorted = MenuCapacityOrdering.sorted(rows)
        let grouped = Dictionary(grouping: sorted) { "\($0.providerID)|\($0.accountRef)" }
        return grouped.map { Self(id: $0.key, rows: MenuCapacityOrdering.sorted($0.value)) }.sorted {
            MenuCapacityOrdering.isBefore($0.primary, $1.primary)
        }
    }
}

enum Format {
    static func percent(_ ratio: Double) -> String {
        "\(Int((min(max(ratio, 0), 1) * 100).rounded()))%"
    }

    static func tokens(_ tokens: Int64) -> String {
        let value = Double(tokens)
        if abs(value) >= 1_000_000_000 { return compact(value / 1_000_000_000, suffix: "B") }
        if abs(value) >= 1_000_000 { return compact(value / 1_000_000, suffix: "M") }
        if abs(value) >= 1_000 { return compact(value / 1_000, suffix: "K") }
        return "\(tokens)"
    }

    static func todayTokens(_ tokens: Int64?) -> String {
        tokens.map(Self.tokens) ?? AppLocalization.text("Unavailable")
    }

    static func timestamp(_ value: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fractional.date(from: value) { return date }
        return ISO8601DateFormatter().date(from: value)
    }

    private static func compact(_ value: Double, suffix: String) -> String {
        let text = value >= 100 || value.rounded() == value ? String(format: "%.0f", value) : String(format: "%.1f", value)
        return text + suffix
    }

    static func provider(_ id: String) -> String {
        ProviderCatalog.descriptor(for: id).displayName
    }

    static func window(_ value: String) -> String {
        AppLocalization.text(
            value.replacingOccurrences(of: "_", with: " ")
                .replacingOccurrences(of: "-", with: " ").capitalized
        )
    }

    static func reset(_ value: String?, now: Date) -> String {
        guard let value, let date = timestamp(value) else {
            return AppLocalization.text("Reset unavailable")
        }
        let seconds = Int(date.timeIntervalSince(now))
        guard seconds > 0 else { return AppLocalization.text("Reset due") }
        if seconds < 3_600 {
            return AppLocalization.format("resets in %lldm", Int64(max(1, seconds / 60)))
        }
        if seconds < 86_400 {
            return AppLocalization.format("resets in %lldh", Int64(max(1, seconds / 3_600)))
        }
        return AppLocalization.format("resets in %lldd", Int64(max(1, seconds / 86_400)))
    }

    static func age(_ seconds: Int64) -> String {
        if seconds < 60 { return "\(seconds)s" }
        if seconds < 3_600 { return "\(seconds / 60)m" }
        if seconds < 86_400 { return "\(seconds / 3_600)h" }
        return "\(seconds / 86_400)d"
    }

    static func state(_ value: String) -> String {
        AppLocalization.text(value.replacingOccurrences(of: "_", with: " ").capitalized)
    }

    static func quality(_ value: String) -> String {
        switch value.lowercased() {
        case "derived", "estimated": AppLocalization.text("Estimated")
        case "cached": AppLocalization.text("Cached")
        case "exact", "live": AppLocalization.text("Exact")
        default: state(value)
        }
    }
}
