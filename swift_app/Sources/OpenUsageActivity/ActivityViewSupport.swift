import AppKit
import Foundation
import SwiftUI
import UsageCore

struct DataHealthPage: View {
    let data: ActivityLoadedData
    let retry: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            PageHeading("Data Health", detail: "Sanitized collection status")
            if data.visibilityIssue {
                StatusBanner(symbol: "eye.slash", text: "Provider visibility settings are invalid. All providers remain visible.")
            }
            let integrationSources = data.health.sources.filter { source in
                !OpenUsageCatalogPresentation.isCatalogSource(source)
                    && ProviderCenterPresentation.isSystemIntegration(
                        data.providerDescriptor(for: source.providerID).familyID
                    )
            }
            if OpenUsageCatalogPresentation.from(data.health.sources) != nil
                || !integrationSources.isEmpty {
                Text("System Integrations").font(.title2.weight(.semibold))
            }
            if let catalog = OpenUsageCatalogPresentation.from(data.health.sources) {
                VStack(alignment: .leading, spacing: 7) {
                    HStack {
                        Text("OpenUsage compatibility").font(.headline)
                        Text("Provider catalog").foregroundStyle(.secondary)
                        Spacer()
                        StateLabel(state: catalog.state)
                    }
                    LabeledContent("Status", value: catalog.title)
                    LabeledContent("Providers", value: catalog.countSummary)
                    Text("Diagnostic only; readable provider data remains available.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Divider()
            }
            ForEach(integrationSources, id: \.stableID) { source in
                let issue = ProviderSourceIssuePresentation.make(from: source)
                VStack(alignment: .leading, spacing: 7) {
                    HStack {
                        Text("OpenUsage").font(.headline)
                        Text(issue.title).foregroundStyle(.secondary)
                        Spacer()
                        StateLabel(state: source.effectiveState)
                    }
                    if issue.isIssue {
                        Text(issue.message).font(.callout)
                    } else {
                        Text("OpenUsage data collection is available.").font(.callout)
                    }
                    LabeledContent("Last attempt", value: DateText.display(source.lastAttemptAt))
                    LabeledContent(
                        "Last success",
                        value: source.lastSuccessAt.map(DateText.display) ?? "Unavailable"
                    )
                }
                Divider()
            }
            let providerSources = data.health.sources.filter {
                !OpenUsageCatalogPresentation.isCatalogSource($0)
                    && !ProviderCenterPresentation.isSystemIntegration(
                        data.providerDescriptor(for: $0.providerID).familyID
                    )
            }
            if providerSources.isEmpty
                && integrationSources.isEmpty
                && OpenUsageCatalogPresentation.from(data.health.sources) == nil {
                EmptyDataView(title: "No source status", description: "No collection source has reported status yet.")
            } else {
                if !providerSources.isEmpty {
                    Text("Provider Data Sources").font(.title2.weight(.semibold))
                }
                ForEach(providerSources, id: \.stableID) { source in
                    let descriptor = data.providerDescriptor(for: source.providerID)
                    let runtimeSource = ProviderRuntimeSourcePresentation.resolve(
                        runtimeSourceID: source.sourceID, descriptor: descriptor
                    )
                    VStack(alignment: .leading, spacing: 5) {
                        HStack {
                            Text(descriptor.displayName).font(.headline)
                            Text(runtimeSource.roleTitle).foregroundStyle(.secondary)
                            Spacer()
                            StateLabel(state: source.effectiveState)
                        }
                        if runtimeSource.strategies.isEmpty {
                            LabeledContent("Strategy", value: "Uncatalogued source")
                        } else {
                            LabeledContent("Strategy") {
                                Text(runtimeSource.strategies.map(\.summary).joined(separator: "; "))
                                    .multilineTextAlignment(.trailing)
                            }
                            LabeledContent("Platforms", value: runtimeSource.platforms)
                        }
                        LabeledContent("Last attempt", value: DateText.display(source.lastAttemptAt))
                        LabeledContent("Last success", value: source.lastSuccessAt.map(DateText.display) ?? "Unavailable")
                        if let stale = source.staleAt { LabeledContent("Stale after", value: DateText.display(stale)) }
                        if let code = source.errorCode { LabeledContent("Error code", value: SourceText.errorCode(code)) }
                    }
                    Divider()
                }
            }
            HStack {
                Button("Retry", systemImage: "arrow.clockwise") { retry() }
                Button("Repair in Provider Settings", systemImage: "wrench.and.screwdriver") { SettingsHelper.open() }
            }
        }
    }
}

struct OpenUsageCatalogPresentation: Sendable, Hashable {
    let outcome: String
    let expectedCount: Int
    let actualCount: Int
    let missingCount: Int
    let extraCount: Int

    static let providerID = "openusage_catalog"
    static let sourceID = "openusage.detect"
    private static let outcomes = Set([
        "ok", "openusage_unavailable", "unsupported_openusage_version",
        "provider_catalog_drift", "invalid_detect_output", "timeout",
    ])

    static func isCatalogSource(_ source: SourceHealthItem) -> Bool {
        source.providerID == providerID && source.sourceID == sourceID
    }

    static func globalHealthHasIssues(_ sources: [SourceHealthItem]) -> Bool {
        sources.contains {
            !isCatalogSource($0) && $0.effectiveState.lowercased() != "ok"
        }
    }

    static func from(_ sources: [SourceHealthItem]) -> Self? {
        guard let source = sources.first(where: isCatalogSource) else { return nil }
        if source.state == "ok", source.errorCode == nil {
            return Self(
                outcome: "ok", expectedCount: upstreamCount,
                actualCount: upstreamCount,
                missingCount: 0, extraCount: 0
            )
        }
        guard let code = source.errorCode else { return invalid }
        let expression = try? NSRegularExpression(
            pattern: #"^([a-z_]+)_e([0-9]+)_a([0-9]+)_m([0-9]+)_x([0-9]+)$"#
        )
        let range = NSRange(code.startIndex..<code.endIndex, in: code)
        guard let match = expression?.firstMatch(in: code, range: range),
              match.range == range,
              let outcomeRange = Range(match.range(at: 1), in: code),
              let expectedRange = Range(match.range(at: 2), in: code),
              let actualRange = Range(match.range(at: 3), in: code),
              let missingRange = Range(match.range(at: 4), in: code),
              let extraRange = Range(match.range(at: 5), in: code),
              outcomes.contains(String(code[outcomeRange])),
              let expected = Int(code[expectedRange]),
              let actual = Int(code[actualRange]),
              let missing = Int(code[missingRange]),
              let extra = Int(code[extraRange])
        else { return invalid }
        return Self(
            outcome: String(code[outcomeRange]), expectedCount: expected,
            actualCount: actual, missingCount: missing, extraCount: extra
        )
    }

    private static var invalid: Self {
        Self(
            outcome: "invalid_detect_output", expectedCount: upstreamCount,
            actualCount: 0, missingCount: 0, extraCount: 0
        )
    }

    private static var upstreamCount: Int {
        GeneratedProviderCatalog.upstreamFamilyIDs.count
    }

    var state: String { outcome == "ok" ? "ok" : "diagnostic" }
    var isGlobalFailure: Bool { false }
    var title: String {
        switch outcome {
        case "ok": "Compatible"
        case "openusage_unavailable": "OpenUsage unavailable"
        case "unsupported_openusage_version": "Unsupported OpenUsage version"
        case "provider_catalog_drift": "Provider catalog changed"
        case "timeout": "Compatibility check timed out"
        default: "Compatibility check unavailable"
        }
    }
    var countSummary: String {
        if outcome == "provider_catalog_drift" {
            return "\(actualCount) detected · \(missingCount) missing · \(extraCount) extra"
        }
        return "\(actualCount) of \(expectedCount) detected"
    }
}

struct NoMatchView: View {
    let match: ActivitySelectionMatch
    let providerName: (String) -> String
    let retry: () -> Void
    let clear: () -> Void

    var body: some View {
        ContentUnavailableView {
            Label("No matching usage data", systemImage: "line.3.horizontal.decrease.circle")
        } description: {
            Text(description)
        } actions: {
            Button("Retry", systemImage: "arrow.clockwise", action: retry)
            Button("Clear Filters", systemImage: "xmark.circle", action: clear)
        }
        .frame(maxWidth: .infinity, minHeight: 280)
    }

    private var description: String {
        switch match {
        case .matched: "No matching usage data is available."
        case let .noMatchingProvider(id): "The selected Provider \(providerName(id)) is unavailable or hidden."
        case let .noMatchingModel(id): "The selected model \(DisplayText.model(id)) has no visible activity."
        }
    }
}

struct FailureView: View {
    let error: RepositoryError?
    let retry: () -> Void
    var body: some View {
        ContentUnavailableView {
            Label(error == .databaseUnavailable ? "Usage database unavailable" : "Usage details unavailable", systemImage: "externaldrive.badge.exclamationmark")
        } description: {
            Text(error?.localizedDescription ?? "No matching usage data is available.")
        } actions: {
            Button("Retry") { retry() }
        }
    }
}

struct EmptyDataView: View {
    let title: String
    let description: String
    var body: some View {
        ContentUnavailableView(
            AppLocalization.text(title),
            systemImage: "chart.bar.xaxis",
            description: Text(AppLocalization.text(description))
        )
            .frame(maxWidth: .infinity, minHeight: 140)
    }
}

struct PageHeading: View {
    let title: String
    let detail: String
    init(_ title: String, detail: String) { self.title = title; self.detail = detail }
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(AppLocalization.text(title)).font(.largeTitle.weight(.semibold))
            Text(AppLocalization.text(detail)).foregroundStyle(.secondary)
        }
    }
}

struct SectionHeading: View {
    let title: String
    let detail: String
    init(_ title: String, detail: String) { self.title = title; self.detail = detail }
    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(AppLocalization.text(title)).font(.title2.weight(.semibold))
            Text(AppLocalization.text(detail)).font(.callout).foregroundStyle(.secondary)
            Spacer()
        }
    }
}

struct StatusBanner: View {
    let symbol: String
    let text: String
    var body: some View {
        Label(text, systemImage: symbol).font(.callout).foregroundStyle(.orange)
            .padding(.vertical, 8)
    }
}

struct StateLabel: View {
    let state: String
    var body: some View {
        Label(SourceText.state(state), systemImage: SourceText.symbol(state))
            .font(.caption).foregroundStyle(SourceText.color(state))
    }
}

enum SettingsHelper {
    @MainActor static func open() {
        let plan = ActivityHelperPlan.settings(
            activityBundleURL: Bundle.main.bundleURL,
            activityExecutableURL: Bundle.main.executableURL ?? Bundle.main.bundleURL
        )
        Task { @MainActor in
            guard await ActivityHelperLaunchService.live.launch(plan) == .launched else {
                showUnavailable()
                return
            }
        }
    }

    @MainActor private static func showUnavailable() {
            let alert = NSAlert()
            alert.messageText = "Provider Settings unavailable"
            alert.informativeText = "Reinstall OpenUsage Bar to restore the settings helper."
            alert.runModal()
    }
}

private enum AccountText {
    static func pseudonymous(_ value: String) -> String {
        guard !value.isEmpty else { return "Default" }
        let hash = value.utf8.reduce(UInt64(14_695_981_039_346_656_037)) { ($0 ^ UInt64($1)) &* 1_099_511_628_211 }
        return "Account " + String(format: "%08llx", hash).suffix(8)
    }
}

enum CapacityText {
    static func value(_ row: CapacityItem) -> String {
        if let ratio = row.remainingRatio { return percentage(ratio) }
        if let remaining = row.remaining { return row.unit == "count" ? remaining : "\(remaining) \(row.unit)" }
        return "Unavailable"
    }

    static func percentage(_ ratio: Double) -> String {
        QuotaHistoryTooltipText.percentage(ratio)
    }
}

enum APISpendText {
    static func display(amount: Decimal, currency: String) -> String {
        let formatter = NumberFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.numberStyle = .decimal
        formatter.usesGroupingSeparator = false
        formatter.minimumFractionDigits = 2
        formatter.maximumFractionDigits = 6
        let number = formatter.string(from: NSDecimalNumber(decimal: amount))
            ?? NSDecimalNumber(decimal: amount).stringValue
        return "\(currency) \(number)"
    }
}

enum DateText {
    static func display(_ value: String) -> String {
        guard let date = ActivityTimestamp.date(from: value) else { return "Unavailable" }
        return date.formatted(date: .abbreviated, time: .shortened)
    }
    static func reset(_ value: String?) -> String { value.map { "Resets \(display($0))" } ?? "Reset unavailable" }
    static func month(_ yearMonth: String) -> String {
        let parts = yearMonth.split(separator: "-")
        guard parts.count == 2, let month = Int(parts[1]), (1...12).contains(month) else { return "" }
        return Calendar.current.shortMonthSymbols[month - 1]
    }
}

private enum SourceText {
    static func state(_ value: String) -> String { value.replacingOccurrences(of: "_", with: " ").capitalized }
    static func errorCode(_ value: String) -> String {
        value.range(of: #"^[A-Za-z0-9._-]{1,80}$"#, options: .regularExpression) == nil
            ? "unknown_error" : value
    }
    static func symbol(_ value: String) -> String {
        switch value { case "ok": "checkmark.circle"; case "stale": "clock.badge.exclamationmark"; default: "exclamationmark.triangle" }
    }
    static func color(_ value: String) -> Color { value == "ok" ? .secondary : .orange }
}

extension UsageDetailsRoute {
    var title: String {
        switch self {
        case .activity: AppLocalization.text("Activity")
        case .capacity: AppLocalization.text("Capacity")
        case .apiSpend: AppLocalization.text("API Spend")
        case .localTools: AppLocalization.text("Local Tools")
        case .providersAndAccounts: AppLocalization.text("Providers")
        case .dataHealth: AppLocalization.text("Data Health")
        }
    }
    var symbol: String {
        switch self {
        case .activity: "chart.bar.xaxis"; case .capacity: "gauge.with.dots.needle.50percent"
        case .apiSpend: "dollarsign.circle"
        case .localTools: "terminal"; case .providersAndAccounts: "bolt.horizontal.circle"
        case .dataHealth: "waveform.path.ecg"
        }
    }
}

extension ProviderBrowseCategory {
    var title: String {
        switch self {
        case .all: AppLocalization.text("All")
        case .subscription: AppLocalization.text("Plans")
        case .api: "API"
        case .cloud: AppLocalization.text("Cloud")
        case .local: AppLocalization.text("Local")
        }
    }

    var symbol: String {
        switch self {
        case .all: "square.grid.2x2"
        case .subscription: "gauge.with.dots.needle.50percent"
        case .api: "key"
        case .cloud: "cloud"
        case .local: "terminal"
        }
    }

    var color: Color {
        .secondary
    }
}

extension ProviderConnectionStatus {
    var sortRank: Int {
        switch self { case .attention: 0; case .connected: 1; case .available: 2 }
    }

    var title: String {
        switch self {
        case .available: AppLocalization.text("Available")
        case .connected: AppLocalization.text("Connected")
        case .attention: AppLocalization.text("Needs attention")
        }
    }

    var symbol: String {
        switch self { case .available: "circle"; case .connected: "checkmark.circle.fill"; case .attention: "exclamationmark.triangle.fill" }
    }

    var color: Color {
        switch self { case .available: .secondary; case .connected: .green; case .attention: .orange }
    }
}

extension ProviderCapabilityState {
    var symbol: String {
        switch self { case .supported: "checkmark"; case .unsupported: "minus"; case .unknown: "questionmark" }
    }

    var color: Color {
        switch self { case .supported: .green; case .unsupported: .secondary; case .unknown: .orange }
    }
}

extension UsagePeriod {
    var title: String { rawValue.capitalized }
}

extension ActivityQuality {
    var symbol: String {
        switch self { case .exact: "checkmark.seal"; case .estimated: "function"; case .partial: "circle.lefthalf.filled"; case .missing: "questionmark.circle" }
    }
}

extension ProviderProductCategory {
    var title: String { self == .localTool ? "Local Tool" : rawValue.capitalized }
}

extension ProviderMetricFamily {
    var title: String {
        switch self { case .subscriptionQuota: "Subscription Quota"; case .tokenActivity: "Token Activity"; case .billing: "Billing"; case .operational: "Operational" }
    }
}

extension CredentialSourceType {
    var title: String {
        switch self {
        case .none: "Provider owned"
        case .keychain: "Keychain"
        case .browserSession: "Browser Session"
        case .apiKey: "API Key"
        case .oauth: "OAuth"
        case .cli: "CLI"
        case .local: "Local"
        }
    }
}

extension APISpendQuality {
    var title: String {
        switch self { case .reported: "Reported"; case .estimated: "Estimated"; case .partial: "Observed, partial" }
    }
    var symbol: String {
        switch self { case .reported: "checkmark.seal"; case .estimated: "function"; case .partial: "circle.lefthalf.filled" }
    }
}

extension SourceHealthItem {
    var stableID: String { "\(providerID)|\(sourceID)" }
}

extension MoveCommandDirection {
    var heatmapDirection: HeatmapDirection? {
        switch self { case .up: .up; case .down: .down; case .left: .left; case .right: .right; @unknown default: nil }
    }
}
