import AppKit
import Foundation
import Observation
import UsageCore

struct ActivityLoadRequest: Sendable, Hashable {
    let period: UsagePeriod
    let ending: LocalDay
    let providerIDs: Set<String>
    let modelIDs: Set<String>
    var repositoryRange: ClosedRange<LocalDay> { UsagePeriod.year.repositoryRange(ending: ending) }
    var metricRange: ClosedRange<LocalDay> { period.range(ending: ending) }
    var signature: String {
        [period.rawValue, ending.rawValue, providerIDs.sorted().joined(separator: ","), modelIDs.sorted().joined(separator: ",")]
            .joined(separator: "|")
    }
}

struct ActivityLoadGate: Sendable, Hashable {
    private var generation = 0

    mutating func begin() -> Int {
        generation += 1
        return generation
    }

    mutating func cancel() { generation += 1 }
    func canPublish(_ candidate: Int) -> Bool { candidate == generation }
}

enum HeatmapGeometry {
    static let cellSize: CGFloat = 13
    static let rowCount = 7
    static let spacing: CGFloat = 4
}

enum HeatmapScrollTarget {
    static func latestPosition(in layout: HeatmapCalendarLayout) -> Int? {
        layout.slots.last { $0.detail != nil }?.position
    }
}

enum HeatmapPointerTarget {
    static func position(x: CGFloat, y: CGFloat, slotCount: Int) -> Int? {
        guard x >= 0, y >= 0, slotCount > 0 else { return nil }
        let pitch = HeatmapGeometry.cellSize + HeatmapGeometry.spacing
        let column = Int(x / pitch)
        let row = Int(y / pitch)
        guard x.truncatingRemainder(dividingBy: pitch) < HeatmapGeometry.cellSize,
              y.truncatingRemainder(dividingBy: pitch) < HeatmapGeometry.cellSize,
              row < HeatmapGeometry.rowCount
        else { return nil }
        let position = (column * HeatmapGeometry.rowCount) + row
        return position < slotCount ? position : nil
    }
}

struct HeatmapTooltipText: Sendable, Hashable {
    let title: String
    let value: String
    let metadata: String
    let accessibilityValue: String

    init(_ day: HeatmapDayDetail) {
        title = day.activity.day.rawValue
        value = switch day.activity.state {
        case .missing: "No collection data"
        case .partial: "\(TokenText.compact(day.activity.observedTokens)) observed Tokens"
        case .coveredZero: "0 Tokens"
        case .coveredActive: "\(TokenText.compact(day.activity.totalTokens ?? 0)) Tokens"
        }
        metadata = [
            day.quality.displayName,
            day.activity.state == .partial ? "Partial" : nil,
            day.isStale ? "Stale" : nil,
            day.lastCollectionAt.map { "Collected \($0)" },
        ].compactMap { $0 }.joined(separator: " · ")
        accessibilityValue = [title, value, metadata].filter { !$0.isEmpty }.joined(separator: ", ")
    }
}

enum DetailsCopy {
    static let sidebar = [
        "Activity", "Capacity", "API Spend", "Local Tools",
        "Providers", "Data Health",
    ]
    static let visibleText = sidebar + [
        "Usage Details", "Refresh", "All Providers", "All Models", "Total Tokens",
        "Peak Tokens", "Active Days", "Current Streak", "Longest Streak",
        "Daily Token Activity", "Daily Model Trend", "Subscription Capacity",
        "Manage Credentials", "Retry", "Missing", "Covered zero",
        "Lower", "Higher", "Exact", "Estimated", "Partial", "Partial history", "Stale",
    ]
}

enum ProviderBrowseCategory: String, CaseIterable, Sendable, Hashable, Identifiable {
    case all, subscription, api, cloud, local

    var id: String { rawValue }

    static func classify(_ descriptor: ProviderDisplayDescriptor) -> Self {
        if ["alibaba_cloud", "azure_openai"].contains(descriptor.familyID) { return .cloud }
        return switch descriptor.category {
        case .subscription: .subscription
        case .api: .api
        case .localTool: .local
        }
    }
}

enum ProviderConnectionStatus: Sendable, Hashable {
    case available, connected, attention
}

struct ProviderSourceIssuePresentation: Sendable, Hashable, Identifiable {
    let providerID: String
    let sourceID: String
    let effectiveState: String
    let errorCode: String?
    let lastSuccessAt: String?

    var id: String { "\(providerID):\(sourceID)" }
    var isIssue: Bool { !["ok", "available"].contains(effectiveState.lowercased()) }

    var requiresUserAction: Bool {
        guard isIssue else { return false }
        let signals = [effectiveState, errorCode ?? ""].map { $0.lowercased() }
        return signals.contains { value in
            value.hasPrefix("auth_")
                || value.contains("credential")
                || value == "login_required"
                || value == "session_expired"
                || value == "unauthorized"
                || value == "forbidden"
        }
    }

    var title: String {
        switch sourceID {
        case "openusage.daily": AppLocalization.text("Daily token history")
        case "current.quota": AppLocalization.text("Current quota")
        case "minimax.billing": AppLocalization.text("Billing usage")
        case "openusage.detect": AppLocalization.text("Provider compatibility")
        default: sourceID.replacingOccurrences(of: ".", with: " ").capitalized
        }
    }

    var message: String {
        if requiresUserAction {
            return "\(title)\(AppLocalization.text(" needs a valid connection."))"
        }
        if effectiveState.lowercased() == "stale" {
            return "\(title)\(AppLocalization.text(" is stale."))"
        }
        if errorCode?.lowercased() == "timeout" {
            return "\(title)\(AppLocalization.text(" refresh timed out."))"
        }
        return "\(title)\(AppLocalization.text(" is temporarily unavailable."))"
    }

    static func make(from source: SourceHealthItem) -> Self {
        Self(
            providerID: source.providerID,
            sourceID: source.sourceID,
            effectiveState: source.effectiveState,
            errorCode: source.errorCode,
            lastSuccessAt: source.lastSuccessAt
        )
    }
}

struct ProviderCenterItem: Identifiable, Sendable, Hashable {
    let descriptor: ProviderDisplayDescriptor
    let instanceCount: Int
    let observed: Bool
    let issues: [ProviderSourceIssuePresentation]

    var id: String { descriptor.familyID }
    var category: ProviderBrowseCategory { .classify(descriptor) }
    var connectionIssues: [ProviderSourceIssuePresentation] {
        issues.filter { $0.requiresUserAction }
    }
    var secondaryIssues: [ProviderSourceIssuePresentation] {
        issues.filter { $0.isIssue && !$0.requiresUserAction }
    }
    var status: ProviderConnectionStatus {
        if !connectionIssues.isEmpty { return .attention }
        return observed || instanceCount > 0 ? .connected : .available
    }
    var helpText: String {
        if let issue = connectionIssues.first ?? secondaryIssues.first { return issue.message }
        return switch status {
        case .available: "Available"
        case .connected: "Connected"
        case .attention: "Needs attention"
        }
    }
}

enum ProviderCenterPresentation {
    static func isSystemIntegration(_ familyID: String) -> Bool {
        ["openusage", "openusage_catalog"].contains(familyID)
    }

    static func filter(
        _ items: [ProviderCenterItem], category: ProviderBrowseCategory, query: String
    ) -> [ProviderCenterItem] {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return items.filter { item in
            (category == .all || item.category == category)
                && (needle.isEmpty
                    || item.descriptor.displayName.localizedCaseInsensitiveContains(needle)
                    || item.descriptor.familyID.localizedCaseInsensitiveContains(needle))
        }
    }

    static func selection(current: String?, visibleIDs: [String]) -> String? {
        guard let current, visibleIDs.contains(current) else { return visibleIDs.first }
        return current
    }
}

enum ProviderCenterText {
    static func region(_ value: String) -> String {
        switch value {
        case "cn", "china": AppLocalization.text("China")
        case "international": AppLocalization.text("International")
        case "Configured": AppLocalization.text("Configured")
        default: value.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    static func scope(_ descriptor: ProviderDisplayDescriptor) -> String? {
        let regions = descriptor.regions
        if regions == ["cn", "international"] {
            return AppLocalization.text("China and International")
        }
        if regions == ["cn"] { return AppLocalization.text("China") }
        if regions == ["international"] { return AppLocalization.text("International") }
        return regions.isEmpty ? nil : regions.sorted().joined(separator: ", ")
    }

    static func connectionMethod(_ descriptor: ProviderDisplayDescriptor) -> String {
        let sources = descriptor.credentialSourceTypes
        if sources.contains(.browserSession), sources.contains(.apiKey) {
            return AppLocalization.text("API Key or web session")
        }
        if sources.contains(.apiKey) { return "API Key" }
        if sources.contains(.oauth) || sources.contains(.keychain) || sources.contains(.local) {
            return AppLocalization.text("Existing local login")
        }
        if sources.contains(.cli) { return AppLocalization.text("CLI configuration") }
        return AppLocalization.text("OpenUsage data source")
    }
}

struct ProviderConnectionSummary: Sendable, Hashable, Identifiable {
    let providerID: String
    let familyID: String
    let displayName: String
    let kind: String
    let site: String?

    var id: String { providerID }
    var isStepPlan: Bool { kind == "step_plan" && familyID == "step_plan" }
    var isManaged: Bool {
        ["minimax", "step_plan", "openai_organization", "generic", "daily_usage_feed"]
            .contains(kind)
    }
    var credentialLabel: String {
        switch kind {
        case "minimax": AppLocalization.text("Replacement Coding Plan key")
        case "openai_organization": AppLocalization.text("Replacement Admin API key")
        default: AppLocalization.text("Replacement API key")
        }
    }
    var credentialPlaceholder: String {
        AppLocalization.text("Leave blank to keep the saved credential")
    }
}

enum ProviderConnectionSummaryError: Error { case invalidConfiguration }

struct ProviderConnectionSummaryStore {
    private struct Envelope: Decodable {
        let version: Int
        let providers: [Row]
    }

    private struct Row: Decodable {
        let providerID: String
        let name: String
        let type: String
        let familyID: String?
        let site: String?

        enum CodingKeys: String, CodingKey {
            case name, type, site
            case providerID = "provider_id"
            case familyID = "family_id"
        }
    }

    static let defaultURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".config/openusage-bar/providers.json")

    let url: URL

    init(url: URL = Self.defaultURL) { self.url = url }

    func load() throws -> [ProviderConnectionSummary] {
        guard FileManager.default.fileExists(atPath: url.path) else { return [] }
        let data = try Data(contentsOf: url, options: .mappedIfSafe)
        guard data.count <= 1_048_576 else {
            throw ProviderConnectionSummaryError.invalidConfiguration
        }
        let envelope = try JSONDecoder().decode(Envelope.self, from: data)
        guard envelope.version == 1 else {
            throw ProviderConnectionSummaryError.invalidConfiguration
        }
        return try envelope.providers.map { row in
            guard Self.isStableID(row.providerID),
                  !row.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  row.name.utf8.count <= 160
            else { throw ProviderConnectionSummaryError.invalidConfiguration }
            let familyID = switch row.type {
            case "step_plan": "step_plan"
            case "minimax": "minimax"
            case "openai_organization": "openai"
            case "daily_usage_feed": row.familyID ?? row.providerID
            default: row.providerID
            }
            guard Self.isStableID(familyID) else {
                throw ProviderConnectionSummaryError.invalidConfiguration
            }
            if row.type == "step_plan" && !["china", "international"].contains(row.site) {
                throw ProviderConnectionSummaryError.invalidConfiguration
            }
            return ProviderConnectionSummary(
                providerID: row.providerID,
                familyID: familyID,
                displayName: row.name,
                kind: row.type,
                site: row.site
            )
        }
    }

    private static func isStableID(_ value: String) -> Bool {
        !value.isEmpty && value.utf8.count <= 128
            && value.unicodeScalars.allSatisfy { scalar in
                scalar.isASCII && (
                    CharacterSet.alphanumerics.contains(scalar)
                        || [".", "_", "-"].contains(Character(scalar))
                )
            }
    }
}

struct LocalToolUsageSummary: Identifiable, Sendable, Hashable {
    let providerID: String
    let displayName: String
    let observedTokens: Int64
    let activeDays: Int
    let knownModelIDs: [String]
    let lastActivityDay: LocalDay?
    let lastCollectionAt: String?
    let quality: ActivityQuality
    let state: String
    var id: String { providerID }
}

enum LocalToolUsagePresentation {
    static func make(
        descriptors: [ProviderDisplayDescriptor], periodRecords: [DailyUsage],
        historyRecords: [DailyUsage], health: [SourceHealthItem]
    ) -> [LocalToolUsageSummary] {
        descriptors.map { descriptor in
            let selected = periodRecords.filter { $0.providerID == descriptor.providerID }
            let history = historyRecords.filter { $0.providerID == descriptor.providerID }
            let sources = health.filter { $0.providerID == descriptor.providerID }
            let qualityRows = selected.isEmpty ? history : selected
            let quality: ActivityQuality
            if qualityRows.isEmpty {
                quality = .missing
            } else if qualityRows.contains(where: { $0.quality.lowercased().contains("partial") }) {
                quality = .partial
            } else if qualityRows.allSatisfy({
                ["exact", "direct", "live"].contains($0.quality.lowercased())
            }) {
                quality = .exact
            } else {
                quality = .estimated
            }
            let timestamps = history.map(\.importedAt) + sources.compactMap(\.lastSuccessAt)
            return LocalToolUsageSummary(
                providerID: descriptor.providerID,
                displayName: descriptor.displayName,
                observedTokens: selected.reduce(Int64(0)) { $0 + $1.totalTokens },
                activeDays: Set(selected.filter { $0.totalTokens > 0 }.map(\.day)).count,
                knownModelIDs: Set(history.map(\.modelID)).sorted(),
                lastActivityDay: history.filter { $0.totalTokens > 0 }.map(\.day).max(),
                lastCollectionAt: latestTimestamp(timestamps),
                quality: quality,
                state: sources.first { $0.effectiveState.lowercased() != "ok" }?.effectiveState
                    ?? sources.first?.effectiveState ?? "unavailable"
            )
        }.sorted { left, right in
            left.displayName == right.displayName
                ? left.providerID < right.providerID
                : left.displayName.localizedStandardCompare(right.displayName) == .orderedAscending
        }
    }

    private static func latestTimestamp(_ values: [String]) -> String? {
        values.compactMap { value in
            ActivityTimestamp.date(from: value).map { (value, $0) }
        }.max { $0.1 < $1.1 }?.0
    }
}

enum ActivityHelperTarget: Sendable, Hashable {
    case application(URL)
    case executable(URL)
}

struct ActivityHelperPlan: Sendable, Hashable {
    let target: ActivityHelperTarget
    let arguments: [String]

    static func settings(
        activityBundleURL: URL,
        activityExecutableURL: URL,
        exists: (URL) -> Bool = { FileManager.default.fileExists(atPath: $0.path) },
        isExecutable: (URL) -> Bool = { FileManager.default.isExecutableFile(atPath: $0.path) }
    ) -> Self? {
        let helperDirectory = activityBundleURL.pathExtension.lowercased() == "app"
            ? activityBundleURL.deletingLastPathComponent()
            : activityExecutableURL.deletingLastPathComponent()
        let applicationNames = ["OpenUsage Provider Settings.app", "OpenUsageSettings.app"]
        if let application = applicationNames.map(helperDirectory.appendingPathComponent).first(where: exists) {
            return Self(target: .application(application), arguments: [])
        }
        let executableNames = ["OpenUsageSettings", "openusage_settings"]
        if let executable = executableNames.map(helperDirectory.appendingPathComponent).first(where: isExecutable) {
            return Self(target: .executable(executable), arguments: [])
        }
        return nil
    }
}

struct ProviderMutationCommand: Sendable, Hashable {
    let executableURL: URL
    let arguments: [String]

    static func resolve(
        activityBundleURL: URL,
        activityExecutableURL: URL,
        isExecutable: (URL) -> Bool = { FileManager.default.isExecutableFile(atPath: $0.path) }
    ) -> Self? {
        let helperDirectory = activityBundleURL.pathExtension.lowercased() == "app"
            ? activityBundleURL.deletingLastPathComponent()
            : activityExecutableURL.deletingLastPathComponent()
        let bundled = helperDirectory
            .appendingPathComponent("OpenUsage Provider Settings.app")
            .appendingPathComponent("Contents/MacOS/OpenUsage Provider Settings")
        if isExecutable(bundled) {
            return Self(executableURL: bundled, arguments: ["provider-mutate"])
        }
        let fallbacks = ["OpenUsageSettings", "openusage_settings"].map {
            helperDirectory.appendingPathComponent($0)
        }
        guard let executable = fallbacks.first(where: isExecutable) else { return nil }
        return Self(executableURL: executable, arguments: ["provider-mutate"])
    }
}

struct ProviderEditRequest: Encodable, Sendable, Hashable {
    let version = 1
    let action = "update_connection"
    let providerID: String
    let name: String
    let apiKey: String
    let sessionCookie: String

    enum CodingKeys: String, CodingKey {
        case version, action, name, apiKey, sessionCookie
        case providerID = "providerId"
    }
}

struct ProviderMutationResponse: Decodable, Sendable, Hashable {
    let version: Int
    let ok: Bool
    let message: String
}

enum ProviderMutationFailure: Error, Sendable, Hashable {
    case unavailable, couldNotLaunch, invalidResponse

    var message: String {
        switch self {
        case .unavailable: "Provider editor is unavailable. Reinstall OpenUsage Bar."
        case .couldNotLaunch: "Provider connection could not be updated."
        case .invalidResponse: "Provider editor returned an invalid response."
        }
    }
}

enum ProviderMutationService {
    static func submit(
        _ request: ProviderEditRequest,
        command: ProviderMutationCommand
    ) async -> Result<ProviderMutationResponse, ProviderMutationFailure> {
        await Task.detached(priority: .userInitiated) {
            do {
                let requestData = try JSONEncoder().encode(request)
                let process = Process()
                let input = Pipe()
                let output = Pipe()
                process.executableURL = command.executableURL
                process.arguments = command.arguments
                process.standardInput = input
                process.standardOutput = output
                process.standardError = FileHandle.nullDevice
                try process.run()
                input.fileHandleForWriting.write(requestData)
                try input.fileHandleForWriting.close()
                let responseData = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                guard process.terminationStatus == 0 else {
                    return .failure(.couldNotLaunch)
                }
                let response = try JSONDecoder().decode(
                    ProviderMutationResponse.self, from: responseData
                )
                guard response.version == 1 else { return .failure(.invalidResponse) }
                return .success(response)
            } catch is DecodingError {
                return .failure(.invalidResponse)
            } catch {
                return .failure(.couldNotLaunch)
            }
        }.value
    }
}

enum ActivityHelperLaunchResult: Sendable, Hashable { case launched, unavailable, failed }

@MainActor
private enum ActivityHelperExecutableRunner {
    static var active: [Process] = []
    static func run(_ url: URL, _ arguments: [String]) -> Bool {
        active.removeAll { !$0.isRunning }
        let process = Process()
        process.executableURL = url
        process.arguments = arguments
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
            active.append(process)
            return true
        } catch { return false }
    }
}

@MainActor
struct ActivityHelperLaunchService {
    typealias ApplicationOpener = @MainActor (URL, [String], @escaping @MainActor (Error?) -> Void) -> Void
    typealias ExecutableRunner = @MainActor (URL, [String]) -> Bool
    let openApplication: ApplicationOpener
    let runExecutable: ExecutableRunner

    func launch(_ plan: ActivityHelperPlan?) async -> ActivityHelperLaunchResult {
        guard let plan else { return .unavailable }
        switch plan.target {
        case let .application(url):
            return await withCheckedContinuation { continuation in
                openApplication(url, plan.arguments) { error in
                    continuation.resume(returning: error == nil ? .launched : .failed)
                }
            }
        case let .executable(url):
            return runExecutable(url, plan.arguments) ? .launched : .failed
        }
    }

    static let live = Self(
        openApplication: { url, arguments, completion in
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.arguments = arguments
            NSWorkspace.shared.openApplication(at: url, configuration: configuration) { _, error in
                Task { @MainActor in completion(error) }
            }
        },
        runExecutable: ActivityHelperExecutableRunner.run
    )
}

enum APISpendQuality: String, Sendable, Hashable { case reported, estimated, partial }

struct APISpendTotal: Sendable, Hashable {
    let currency: String
    let amount: Decimal
    let quality: APISpendQuality
}

enum APISpendCoverage: String, Sendable, Hashable { case missing, partial, complete }

struct APISpendSummary: Sendable, Hashable {
    let totals: [APISpendTotal]
    let coverage: APISpendCoverage
}

enum APISpendAggregator {
    static func make(
        costs: DailyCostDataset,
        legacyRecords: [DailyUsage],
        range: ClosedRange<LocalDay>,
        isLegacyCoverageComplete: Bool
    ) -> APISpendSummary {
        let nativeScopes = costs.knownScopes
        let nativeRows = costs.records.filter { range.contains($0.day) }.compactMap {
            row -> (String, Decimal, Bool)? in
            guard let amount = Decimal(
                string: row.amount, locale: Locale(identifier: "en_US_POSIX")
            ) else { return nil }
            return (
                row.currency, amount,
                row.basis.lowercased().contains("estimated")
                    || ["derived", "estimated"].contains(row.quality.lowercased())
            )
        }
        let selectedLegacy = legacyRecords.filter {
            range.contains($0.day)
                && !nativeScopes.contains(ProviderScope(
                    providerID: $0.providerID, accountRef: $0.accountRef
                ))
        }
        let legacyRows = selectedLegacy.compactMap { row -> (String, Decimal, Bool)? in
            guard let currency = row.costCurrency,
                  let raw = row.costAmount,
                  let amount = Decimal(string: raw)
            else { return nil }
            return (currency, amount, row.costBasis?.lowercased().contains("estimated") == true)
        }
        let selectedCoverage = costs.coverage.filter { range.contains($0.day) }
        let expectedNativeCoverage = range.dayCount * nativeScopes.count
        let nativeCoverageComplete = nativeScopes.isEmpty || (
            selectedCoverage.count == expectedNativeCoverage
                && selectedCoverage.allSatisfy(\.isCovered)
        )
        let hasMissingLegacyCost = legacyRows.count != selectedLegacy.count
        let legacyCoverageComplete = selectedLegacy.isEmpty || (
            isLegacyCoverageComplete && !hasMissingLegacyCost
        )
        let allRows = nativeRows + legacyRows
        let totals = Dictionary(grouping: allRows, by: \.0).map { currency, rows in
            let quality: APISpendQuality = !nativeCoverageComplete || !legacyCoverageComplete
                ? .partial : (rows.contains { $0.2 } ? .estimated : .reported)
            return APISpendTotal(
                currency: currency,
                amount: rows.reduce(Decimal.zero) { $0 + $1.1 },
                quality: quality
            )
        }.sorted { $0.currency < $1.currency }
        let hasKnownCoverage = selectedCoverage.contains(where: \.isCovered)
            || !legacyRows.isEmpty
        let coverage: APISpendCoverage = !hasKnownCoverage
            ? .missing
            : (nativeCoverageComplete && legacyCoverageComplete ? .complete : .partial)
        return APISpendSummary(totals: totals, coverage: coverage)
    }
}

struct ActivityFilterSelection: Sendable, Hashable {
    let period: UsagePeriod
    let providerID: String?
    let modelID: String?
}

protocol ActivityPreferencesStore: AnyObject {
    func string(forKey defaultName: String) -> String?
    func set(_ value: Any?, forKey defaultName: String)
    func removeObject(forKey defaultName: String)
}

extension UserDefaults: ActivityPreferencesStore {}

final class ActivityPreferences {
    static let periodKey = "activity.period"
    static let providerKey = "activity.providerID"
    static let modelKey = "activity.modelID"
    private let defaults: any ActivityPreferencesStore

    init(defaults: any ActivityPreferencesStore = UserDefaults.standard) {
        self.defaults = defaults
    }

    func load() -> ActivityFilterSelection {
        let period = defaults.string(forKey: Self.periodKey).flatMap(UsagePeriod.init(rawValue:)) ?? .year
        return ActivityFilterSelection(
            period: period,
            providerID: validID(defaults.string(forKey: Self.providerKey)),
            modelID: validID(defaults.string(forKey: Self.modelKey))
        )
    }

    func save(_ selection: ActivityFilterSelection) {
        defaults.set(selection.period.rawValue, forKey: Self.periodKey)
        set(selection.providerID, forKey: Self.providerKey)
        set(selection.modelID, forKey: Self.modelKey)
    }

    func clearFilters() {
        defaults.removeObject(forKey: Self.providerKey)
        defaults.removeObject(forKey: Self.modelKey)
    }

    private func set(_ value: String?, forKey key: String) {
        if let value, validID(value) != nil { defaults.set(value, forKey: key) }
        else { defaults.removeObject(forKey: key) }
    }

    private func validID(_ value: String?) -> String? {
        guard let value,
              value.range(of: #"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"#, options: .regularExpression) != nil
        else { return nil }
        return value
    }
}

@MainActor
@Observable
final class ActivityWindowRegistry {
    @ObservationIgnored private weak var window: NSWindow?
    @ObservationIgnored private var closeObserver: ActivityNotificationObservation?

    var registeredWindowCount: Int { window == nil ? 0 : 1 }

    func register(_ window: NSWindow) {
        if self.window === window { return }
        removeCloseObserver()
        self.window = window
        let token = NotificationCenter.default.addObserver(
            forName: NSWindow.willCloseNotification,
            object: window, queue: .main
        ) { [weak self, weak window] _ in
            MainActor.assumeIsolated {
                guard let self, let window else { return }
                self.unregister(window)
            }
        }
        closeObserver = ActivityNotificationObservation {
            NotificationCenter.default.removeObserver(token)
        }
    }

    func unregister(_ candidate: NSWindow) {
        guard window === candidate else { return }
        window = nil
        removeCloseObserver()
    }

    func revealExisting() -> Bool {
        guard let window else { return false }
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.makeKeyAndOrderFront(nil)
        return true
    }

    private func removeCloseObserver() {
        closeObserver?.cancel()
        closeObserver = nil
    }

    deinit { closeObserver?.cancel() }
}

@MainActor
@Observable
final class ActivityRouteCoordinator {
    private(set) var route: UsageDetailsRoute
    @ObservationIgnored private let activate: @MainActor () -> Void
    @ObservationIgnored private let revealExistingWindow: @MainActor () -> Bool
    @ObservationIgnored private var openWindow: @MainActor () -> Void
    @ObservationIgnored private var observer: ActivityNotificationObservation?

    init(
        initialRoute: UsageDetailsRoute,
        activate: @escaping @MainActor () -> Void,
        revealExistingWindow: @escaping @MainActor () -> Bool = { false },
        openWindow: @escaping @MainActor () -> Void
    ) {
        route = initialRoute
        self.activate = activate
        self.revealExistingWindow = revealExistingWindow
        self.openWindow = openWindow
    }

    func receive(userInfo: [String: String]) {
        guard let route = ActivityRouteMessage.decode(userInfo) else { return }
        self.route = route
        activate()
        if !revealExistingWindow() { openWindow() }
    }

    func select(_ route: UsageDetailsRoute) { self.route = route }

    func installWindowOpener(_ action: @escaping @MainActor () -> Void) {
        openWindow = action
    }

    func startListening(center: DistributedNotificationCenter = .default()) {
        guard observer == nil else { return }
        let token = center.addObserver(
            forName: ActivityRouteMessage.notificationName,
            object: nil, queue: .main
        ) { [weak self] notification in
            guard let raw = notification.userInfo,
                  raw.count == 1,
                  let route = raw[ActivityRouteMessage.routeKey] as? String
            else { return }
            Task { @MainActor in self?.receive(userInfo: [ActivityRouteMessage.routeKey: route]) }
        }
        observer = ActivityNotificationObservation { center.removeObserver(token) }
    }

    func stopListening(center: DistributedNotificationCenter = .default()) {
        guard let observer else { return }
        _ = center
        observer.cancel()
        self.observer = nil
    }

    deinit { observer?.cancel() }
}

private final class ActivityNotificationObservation: @unchecked Sendable {
    private let cancellation: () -> Void
    private var isCancelled = false

    init(cancellation: @escaping () -> Void) {
        self.cancellation = cancellation
    }

    func cancel() {
        guard !isCancelled else { return }
        isCancelled = true
        cancellation()
    }

    deinit { cancel() }
}

enum ChartFocusClearReason: CaseIterable, Sendable, Hashable {
    case pointerExit, escape, filterChange, windowDeactivation
}

enum ChartFocusSource: Sendable, Hashable {
    case pointer, keyboard
}

struct ChartAnchor: Sendable, Hashable {
    let x: Double
    let y: Double
}

struct ChartSize: Sendable, Hashable {
    let width: Double
    let height: Double
}

struct ChartFocus: Sendable, Hashable {
    var day: LocalDay?
    var source: ChartFocusSource?
    var pointerAnchor: ChartAnchor?

    init(
        day: LocalDay? = nil,
        source: ChartFocusSource? = nil,
        pointerAnchor: ChartAnchor? = nil
    ) {
        self.day = day
        self.source = source
        self.pointerAnchor = pointerAnchor
    }

    mutating func selectPointer(day: LocalDay, anchor: ChartAnchor) {
        self.day = day
        source = .pointer
        pointerAnchor = anchor
    }

    mutating func selectKeyboard(day: LocalDay) {
        self.day = day
        source = .keyboard
        pointerAnchor = nil
    }

    mutating func clear(_ reason: ChartFocusClearReason) {
        _ = reason
        day = nil
        source = nil
        pointerAnchor = nil
    }
}

struct QuotaHistoryPlotTarget: Sendable, Hashable {
    let point: QuotaHistoryPoint
    let anchor: ChartAnchor
}

enum QuotaHistoryNavigationDirection: Sendable, Hashable {
    case left, right
}

enum QuotaHistorySelection {
    static func nearest(
        to location: ChartAnchor, plotOrigin: ChartAnchor, plotSize: ChartSize,
        targets: [QuotaHistoryPlotTarget]
    ) -> QuotaHistoryPoint? {
        guard contains(location, origin: plotOrigin, size: plotSize) else { return nil }
        return targets.filter { $0.anchor.x.isFinite && $0.anchor.y.isFinite }.min { left, right in
            let leftDistance = squaredDistance(from: location, to: left.anchor)
            let rightDistance = squaredDistance(from: location, to: right.anchor)
            if leftDistance != rightDistance { return leftDistance < rightDistance }
            return ordered(left.point, before: right.point)
        }?.point
    }

    static func move(
        from snapshotID: Int64?, direction: QuotaHistoryNavigationDirection,
        points: [QuotaHistoryPoint]
    ) -> QuotaHistoryPoint? {
        let orderedPoints = points.sorted(by: ordered)
        guard !orderedPoints.isEmpty else { return nil }
        guard let snapshotID,
              let current = orderedPoints.firstIndex(where: { $0.snapshotID == snapshotID })
        else { return direction == .right ? orderedPoints.first : orderedPoints.last }
        let destination = switch direction {
        case .left: max(0, current - 1)
        case .right: min(orderedPoints.count - 1, current + 1)
        }
        return orderedPoints[destination]
    }

    private static func contains(
        _ location: ChartAnchor, origin: ChartAnchor, size: ChartSize
    ) -> Bool {
        guard location.x.isFinite, location.y.isFinite,
              origin.x.isFinite, origin.y.isFinite,
              size.width.isFinite, size.height.isFinite,
              size.width > 0, size.height > 0
        else { return false }
        return (origin.x...(origin.x + size.width)).contains(location.x)
            && (origin.y...(origin.y + size.height)).contains(location.y)
    }

    private static func squaredDistance(from left: ChartAnchor, to right: ChartAnchor) -> Double {
        let x = left.x - right.x
        let y = left.y - right.y
        return x * x + y * y
    }

    private static func ordered(_ left: QuotaHistoryPoint, before right: QuotaHistoryPoint) -> Bool {
        if left.observedAt != right.observedAt { return left.observedAt < right.observedAt }
        if left.seriesLabel != right.seriesLabel { return left.seriesLabel < right.seriesLabel }
        return left.snapshotID < right.snapshotID
    }
}

struct QuotaHistoryTooltipText: Sendable, Hashable {
    let title: String
    let remaining: String
    let observed: String
    let reset: String?
    let status: String?
    let statusSymbol: String?
    let accessibilityValue: String
    let detailAccessibilityValue: String

    static func make(
        point: QuotaHistoryPoint,
        formatDate: (Date) -> String = {
            $0.formatted(date: .abbreviated, time: .shortened)
        }
    ) -> Self {
        let remaining = percentage(point.remainingRatio) + " remaining"
        let observedValue = formatDate(point.observedAt)
        let resetValue = point.resetsAt.map(formatDate)
        let status = point.stale ? "Stale" : normalState(point.state)
        let statusSymbol = status.map {
            $0 == "Stale" ? "clock.badge.exclamationmark" : "exclamationmark.triangle"
        }
        let details = [
            remaining.lowercased(), "observed \(observedValue)",
            resetValue.map { "resets \($0)" }, status?.lowercased(),
        ].compactMap { $0 }.joined(separator: ", ")
        return Self(
            title: point.seriesLabel, remaining: remaining,
            observed: "Observed \(observedValue)",
            reset: resetValue.map { "Resets \($0)" }, status: status,
            statusSymbol: statusSymbol,
            accessibilityValue: "\(point.seriesLabel), \(details)",
            detailAccessibilityValue: details
        )
    }

    static func percentage(_ ratio: Double) -> String {
        let value = ratio * 100
        if abs(value - value.rounded()) < 0.05 { return "\(Int(value.rounded()))%" }
        return value.formatted(.number.precision(.fractionLength(1))) + "%"
    }

    private static func normalState(_ state: String) -> String? {
        let normalized = state.lowercased()
        guard normalized != "ok", normalized != "available" else { return nil }
        guard normalized.range(
            of: #"^[a-z0-9_]{1,40}$"#, options: .regularExpression
        ) != nil else { return "Unavailable" }
        return normalized.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

struct QuotaHistoryFocus: Sendable, Hashable {
    var snapshotID: Int64?
    var source: ChartFocusSource?
    var pointerAnchor: ChartAnchor?

    mutating func selectPointer(snapshotID: Int64, anchor: ChartAnchor) {
        self.snapshotID = snapshotID
        source = .pointer
        pointerAnchor = anchor
    }

    mutating func selectKeyboard(snapshotID: Int64) {
        self.snapshotID = snapshotID
        source = .keyboard
        pointerAnchor = nil
    }

    mutating func clear(_ reason: ChartFocusClearReason) {
        _ = reason
        snapshotID = nil
        source = nil
        pointerAnchor = nil
    }
}

enum QuotaHistoryTooltipGeometry {
    static func placementSize(measured: ChartSize) -> ChartSize {
        ChartSize(
            width: measured.width.isFinite ? max(220, measured.width) : 220,
            height: measured.height.isFinite ? max(112, measured.height) : 112
        )
    }
}

enum TooltipPlacement {
    static func center(
        anchor: ChartAnchor,
        tooltip: ChartSize,
        container: ChartSize,
        offset: Double = 12,
        padding: Double = 8
    ) -> ChartAnchor {
        let containerWidth = finiteNonnegative(container.width)
        let containerHeight = finiteNonnegative(container.height)
        let tooltipWidth = finiteNonnegative(tooltip.width)
        let tooltipHeight = finiteNonnegative(tooltip.height)
        let horizontalOffset = finiteNonnegative(offset)
        let edgePadding = finiteNonnegative(padding)
        let anchorX = anchor.x.isFinite ? anchor.x : containerWidth / 2
        let anchorY = anchor.y.isFinite ? anchor.y : containerHeight / 2
        let halfWidth = tooltipWidth / 2
        let halfHeight = tooltipHeight / 2

        var x = anchorX + horizontalOffset + halfWidth
        if x + halfWidth > containerWidth - edgePadding {
            x = anchorX - horizontalOffset - halfWidth
        }
        var y = anchorY - horizontalOffset - halfHeight
        if y - halfHeight < edgePadding {
            y = anchorY + horizontalOffset + halfHeight
        }

        return ChartAnchor(
            x: clampedCenter(
                x, lower: edgePadding + halfWidth,
                upper: containerWidth - edgePadding - halfWidth,
                fallback: containerWidth / 2
            ),
            y: clampedCenter(
                y, lower: edgePadding + halfHeight,
                upper: containerHeight - edgePadding - halfHeight,
                fallback: containerHeight / 2
            )
        )
    }

    private static func finiteNonnegative(_ value: Double) -> Double {
        value.isFinite ? max(0, value) : 0
    }

    private static func clampedCenter(
        _ value: Double, lower: Double, upper: Double, fallback: Double
    ) -> Double {
        guard lower <= upper else { return fallback }
        return min(max(value.isFinite ? value : fallback, lower), upper)
    }
}

enum KeyboardBarAnchor {
    static func centerX(index: Int, count: Int, plotWidth: Double) -> Double? {
        guard count > 0, index >= 0, index < count,
              plotWidth.isFinite, plotWidth > 0
        else { return nil }
        return (Double(index) + 0.5) * plotWidth / Double(count)
    }
}

enum TooltipAppearance: Sendable, Hashable {
    case opacity, immediate
}

enum TooltipMotionPolicy {
    static func appearance(reduceMotion: Bool) -> TooltipAppearance {
        reduceMotion ? .immediate : .opacity
    }
}

enum QuotaHistoryVisualStyle {
    private static let patterns: [[Double]] = [
        [], [8, 4], [2, 3], [10, 4, 2, 4],
        [1, 3], [12, 4], [6, 3, 1, 3], [3, 2, 9, 2],
    ]

    static func dash(_ index: Int) -> [Double] {
        let normalized = (index % patterns.count + patterns.count) % patterns.count
        return patterns[normalized]
    }

    static func colorIndex(styleKey: String) -> Int {
        Int(hash(styleKey) % UInt64(patterns.count))
    }

    static func dashIndex(styleKey: String) -> Int {
        Int((hash(styleKey) / UInt64(patterns.count)) % UInt64(patterns.count))
    }

    private static func hash(_ styleKey: String) -> UInt64 {
        styleKey.split(separator: " ").last.flatMap { UInt64($0, radix: 16) } ?? 0
    }
}

enum QuotaHistorySeriesMode: Sendable, Hashable {
    case focused
    case showAll
    case custom(Set<String>)
}

enum QuotaHistorySeriesVisibility {
    static func defaultVisibleIDs(
        in series: [QuotaHistorySeriesPresentation], maxVisible: Int = 6
    ) -> Set<String> {
        guard maxVisible > 0 else { return [] }
        return Set(series.sorted {
            if $0.currentRatio != $1.currentRatio { return $0.currentRatio < $1.currentRatio }
            if $0.seriesLabel != $1.seriesLabel { return $0.seriesLabel < $1.seriesLabel }
            return $0.seriesID < $1.seriesID
        }.prefix(maxVisible).map(\.seriesID))
    }

    static func toggled(
        _ seriesID: String, visible: Set<String>, allSeries: Set<String>
    ) -> Set<String> {
        var result = visible.intersection(allSeries)
        if result.contains(seriesID) {
            guard result.count > 1 else { return result }
            result.remove(seriesID)
        } else if allSeries.contains(seriesID) {
            result.insert(seriesID)
        }
        return result
    }

    static func visibleIDs(
        mode: QuotaHistorySeriesMode,
        in series: [QuotaHistorySeriesPresentation], maxVisible: Int = 6
    ) -> Set<String> {
        let all = Set(series.map(\.seriesID))
        switch mode {
        case .focused:
            return defaultVisibleIDs(in: series, maxVisible: maxVisible)
        case .showAll:
            return all
        case let .custom(ids):
            let retained = ids.intersection(all)
            return retained.isEmpty
                ? defaultVisibleIDs(in: series, maxVisible: maxVisible)
                : retained
        }
    }
}

enum QuotaHistoryAxisMode: Sendable, Hashable {
    case hours, dateHours, days
}

enum QuotaHistoryAxisText {
    static func mode(for span: TimeInterval) -> QuotaHistoryAxisMode {
        if span <= 24 * 60 * 60 { return .hours }
        if span <= 72 * 60 * 60 { return .dateHours }
        return .days
    }

    static func label(
        _ date: Date, mode: QuotaHistoryAxisMode, calendar: Calendar = .current
    ) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        switch mode {
        case .hours:
            if calendar.component(.hour, from: date) == 0 {
                formatter.dateFormat = "MMM d\nHH:mm"
            } else {
                formatter.dateFormat = "HH:mm"
            }
        case .dateHours:
            formatter.dateFormat = "MMM d\nHH:mm"
        case .days:
            formatter.dateFormat = "MMM d"
        }
        return formatter.string(from: date)
    }
}

struct QuotaHistoryTimeDomain: Sendable, Hashable {
    let lowerBound: Date
    let upperBound: Date

    var span: TimeInterval { upperBound.timeIntervalSince(lowerBound) }
    var range: ClosedRange<Date> { lowerBound...upperBound }

    static func make(points: [QuotaHistoryPoint]) -> QuotaHistoryTimeDomain? {
        guard let first = points.map(\.observedAt).min(),
              let last = points.map(\.observedAt).max()
        else { return nil }
        if first == last {
            return QuotaHistoryTimeDomain(
                lowerBound: first.addingTimeInterval(-60),
                upperBound: last.addingTimeInterval(60)
            )
        }
        return QuotaHistoryTimeDomain(lowerBound: first, upperBound: last)
    }
}

enum QuotaHistoryLegendText {
    static func accessibilityValue(percentage: String, stale: Bool) -> String {
        "\(percentage) remaining" + (stale ? ", stale" : "")
    }
}

enum ActivityPaths {
    static let stateDirectory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".local/state/openusage-bar", isDirectory: true)
    static let ledger = stateDirectory.appendingPathComponent("activity.sqlite3")
    static let visibility = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".config/openusage-bar/visibility.json")
}
