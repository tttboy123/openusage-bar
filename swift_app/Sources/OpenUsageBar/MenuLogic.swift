import Foundation
import UsageCore

public struct StatusLabel: Sendable, Hashable {
    public let values: [String]
    public let accessibilityTitle: String
    public let accessibilityValue: String

    public var text: String? { values.isEmpty ? nil : values.joined(separator: " ") }

    public static func compact(remainingRatio: Double?) -> Self {
        let values = remainingRatio.map { [Format.percent($0)] } ?? []
        return Self(values: values, accessibilityTitle: "OpenUsage Bar", accessibilityValue: values.first.map { "Most urgent capacity, \($0) remaining" } ?? "Capacity unavailable")
    }

    public static func activity(tokens: Int64) -> Self {
        let value = Format.tokens(tokens)
        return Self(values: [value], accessibilityTitle: "OpenUsage Bar", accessibilityValue: "Today Token, \(value)")
    }

    public static func custom(values: [String]) -> Self {
        let short = Array(values.filter { !$0.isEmpty }.prefix(2))
        return Self(values: short, accessibilityTitle: "OpenUsage Bar", accessibilityValue: short.isEmpty ? "Usage unavailable" : short.joined(separator: ", "))
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
        case .failed: "Refresh failed. Showing last-good data."
        case .timedOut: "Refresh timed out. Showing last-good data."
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
        coverage = tokens != nil && !isComplete ? "Partial" : nil
        accessibilityValue = [value, coverage].compactMap { $0 }.joined(separator: ", ")
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
            capacity = "Unavailable"
        }
        reset = Format.reset(row.resetsAt, now: now)
        if row.stale {
            freshness = "Stale, \(Format.age(row.freshnessSeconds)) old"
            stateSymbol = "clock.badge.exclamationmark"
        } else if row.state != "ok" {
            freshness = Format.state(row.state)
            stateSymbol = "exclamationmark.triangle.fill"
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
        case .critical: riskText = "Critical capacity"
        case .warning: riskText = "Warning capacity"
        case .normal: riskText = "Capacity normal"
        }
        visibleMetadata = [reset, freshness].compactMap { $0 }.filter { !$0.isEmpty }
        accessibilityLabel = "\(provider), \(window)"
        accessibilityValue = [window, "\(capacity) remaining", reset, freshness, qualityText, riskText]
            .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: ", ")
    }
}

public struct ProviderCapacityGroup: Sendable, Hashable, Identifiable {
    public let id: String
    public let rows: [CapacityItem]
    public var primary: CapacityItem { rows[0] }
    public var secondary: ArraySlice<CapacityItem> { rows.dropFirst() }

    public static func make(from rows: [CapacityItem]) -> [Self] {
        let sorted = CapacityViewModel.sorted(rows)
        let grouped = Dictionary(grouping: sorted) { "\($0.providerID)|\($0.accountRef)" }
        return grouped.map { Self(id: $0.key, rows: CapacityViewModel.sorted($0.value)) }.sorted {
            CapacityViewModel.sorted([$0.primary, $1.primary]).first == $0.primary
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
        tokens.map(Self.tokens) ?? "No data"
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
        value.replacingOccurrences(of: "_", with: " ").replacingOccurrences(of: "-", with: " ").capitalized
    }

    static func reset(_ value: String?, now: Date) -> String {
        guard let value, let date = timestamp(value) else { return "Reset unavailable" }
        let seconds = Int(date.timeIntervalSince(now))
        guard seconds > 0 else { return "Reset due" }
        if seconds < 3_600 { return "resets in \(max(1, seconds / 60))m" }
        if seconds < 86_400 { return "resets in \(max(1, seconds / 3_600))h" }
        return "resets in \(max(1, seconds / 86_400))d"
    }

    static func age(_ seconds: Int64) -> String {
        if seconds < 60 { return "\(seconds)s" }
        if seconds < 3_600 { return "\(seconds / 60)m" }
        if seconds < 86_400 { return "\(seconds / 3_600)h" }
        return "\(seconds / 86_400)d"
    }

    static func state(_ value: String) -> String {
        value.replacingOccurrences(of: "_", with: " ").capitalized
    }

    static func quality(_ value: String) -> String {
        switch value.lowercased() {
        case "derived", "estimated": "Estimated"
        case "cached": "Cached"
        case "exact", "live": "Exact"
        default: state(value)
        }
    }
}
