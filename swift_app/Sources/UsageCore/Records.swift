import Foundation

public enum LocalDayError: Error, Sendable, Hashable {
    case invalidFormat
}

public struct LocalDay: RawRepresentable, Sendable, Hashable, Comparable, CustomStringConvertible {
    public let rawValue: String

    public init(_ rawValue: String) throws {
        guard rawValue.count == 10 else { throw LocalDayError.invalidFormat }
        let bytes = Array(rawValue.utf8)
        guard bytes[4] == 45, bytes[7] == 45,
              bytes.enumerated().allSatisfy({ index, byte in
                  index == 4 || index == 7 || (48...57).contains(byte)
              }),
              let year = Int(rawValue.prefix(4)),
              let month = Int(rawValue.dropFirst(5).prefix(2)),
              let day = Int(rawValue.suffix(2))
        else { throw LocalDayError.invalidFormat }

        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let components = DateComponents(calendar: calendar, timeZone: calendar.timeZone, year: year, month: month, day: day)
        guard let date = calendar.date(from: components) else { throw LocalDayError.invalidFormat }
        let checked = calendar.dateComponents([.year, .month, .day], from: date)
        guard checked.year == year, checked.month == month, checked.day == day else {
            throw LocalDayError.invalidFormat
        }
        self.rawValue = rawValue
    }

    public init?(rawValue: String) {
        try? self.init(rawValue)
    }

    public var description: String { rawValue }
    public static func < (lhs: Self, rhs: Self) -> Bool { lhs.rawValue < rhs.rawValue }
}

public struct ProviderScope: Sendable, Hashable {
    public let providerID: String
    public let accountRef: String

    public init(providerID: String, accountRef: String) {
        self.providerID = providerID
        self.accountRef = accountRef
    }
}

public struct ProviderInstanceRecord: Sendable, Hashable, Identifiable {
    public let providerID: String
    public let familyID: String
    public let displayName: String
    public let category: ProviderProductCategory
    public let credentialSource: String
    public let sourceKind: String
    public let observedAt: String
    public let revision: Int64
    public var id: String { providerID }

    public init(
        providerID: String, familyID: String, displayName: String,
        category: ProviderProductCategory, credentialSource: String,
        sourceKind: String, observedAt: String, revision: Int64
    ) {
        self.providerID = providerID
        self.familyID = familyID
        self.displayName = displayName
        self.category = category
        self.credentialSource = credentialSource
        self.sourceKind = sourceKind
        self.observedAt = observedAt
        self.revision = revision
    }

    public var descriptor: ProviderDisplayDescriptor {
        ProviderCatalog.descriptor(
            for: providerID, familyID: familyID,
            displayName: displayName, category: category
        )
    }
}

public struct DailyUsage: Sendable, Hashable {
    public let day: LocalDay
    public let providerID: String
    public let accountRef: String
    public let modelID: String
    public let inputTokens: Int64
    public let outputTokens: Int64
    public let cacheReadTokens: Int64
    public let cacheCreationTokens: Int64
    public let reasoningTokens: Int64?
    public let totalTokens: Int64
    public let costAmount: String?
    public let costCurrency: String?
    public let costBasis: String?
    public let quality: String
    public let importedAt: String
    public let revision: Int64
    public let recordID: String
    public let sourceID: String

    public init(
        day: LocalDay, providerID: String, accountRef: String, modelID: String,
        inputTokens: Int64, outputTokens: Int64, cacheReadTokens: Int64,
        cacheCreationTokens: Int64, reasoningTokens: Int64?, totalTokens: Int64,
        costAmount: String?, costCurrency: String?, costBasis: String?, quality: String,
        importedAt: String, revision: Int64, recordID: String,
        sourceID: String = "legacy"
    ) {
        self.day = day
        self.providerID = providerID
        self.accountRef = accountRef
        self.modelID = modelID
        self.inputTokens = inputTokens
        self.outputTokens = outputTokens
        self.cacheReadTokens = cacheReadTokens
        self.cacheCreationTokens = cacheCreationTokens
        self.reasoningTokens = reasoningTokens
        self.totalTokens = totalTokens
        self.costAmount = costAmount
        self.costCurrency = costCurrency
        self.costBasis = costBasis
        self.quality = quality
        self.importedAt = importedAt
        self.revision = revision
        self.recordID = recordID
        self.sourceID = sourceID
    }
}

public struct CoverageDay: Sendable, Hashable {
    public let day: LocalDay
    public let providerID: String
    public let accountRef: String
    public let isCovered: Bool
    public let sourceID: String?

    public init(
        day: LocalDay, providerID: String, accountRef: String,
        isCovered: Bool, sourceID: String? = nil
    ) {
        self.day = day
        self.providerID = providerID
        self.accountRef = accountRef
        self.isCovered = isCovered
        self.sourceID = sourceID
    }
}

public struct ActivityDataset: Sendable, Hashable {
    public let records: [DailyUsage]
    public let coverage: [CoverageDay]
    public let knownScopes: Set<ProviderScope>
    public let revision: Int64

    public init(records: [DailyUsage], coverage: [CoverageDay], knownScopes: Set<ProviderScope>, revision: Int64) {
        self.records = records
        self.coverage = coverage
        self.knownScopes = knownScopes
        self.revision = revision
    }
}

public struct DailyCost: Sendable, Hashable {
    public let day: LocalDay
    public let providerID: String
    public let accountRef: String
    public let costKind: String
    public let currency: String
    public let amount: String
    public let basis: String
    public let quality: String
    public let importedAt: String
    public let revision: Int64
    public let recordID: String

    public init(
        day: LocalDay, providerID: String, accountRef: String,
        costKind: String, currency: String, amount: String,
        basis: String, quality: String, importedAt: String,
        revision: Int64, recordID: String
    ) {
        self.day = day
        self.providerID = providerID
        self.accountRef = accountRef
        self.costKind = costKind
        self.currency = currency
        self.amount = amount
        self.basis = basis
        self.quality = quality
        self.importedAt = importedAt
        self.revision = revision
        self.recordID = recordID
    }
}

public struct CostCoverageDay: Sendable, Hashable {
    public let day: LocalDay
    public let providerID: String
    public let accountRef: String
    public let isCovered: Bool

    public init(day: LocalDay, providerID: String, accountRef: String, isCovered: Bool) {
        self.day = day
        self.providerID = providerID
        self.accountRef = accountRef
        self.isCovered = isCovered
    }
}

public struct DailyCostDataset: Sendable, Hashable {
    public let records: [DailyCost]
    public let coverage: [CostCoverageDay]
    public let knownScopes: Set<ProviderScope>
    public let revision: Int64

    public init(
        records: [DailyCost], coverage: [CostCoverageDay],
        knownScopes: Set<ProviderScope>, revision: Int64
    ) {
        self.records = records
        self.coverage = coverage
        self.knownScopes = knownScopes
        self.revision = revision
    }
}

public enum QuotaScopeKind: String, Sendable, Hashable {
    case subscription
    case account
    case model
}

public struct QuotaAppliesTo: Sendable, Hashable {
    public let kind: QuotaScopeKind
    public let modelIDs: [String]

    public init(kind: QuotaScopeKind, modelIDs: [String] = []) {
        self.kind = kind
        self.modelIDs = modelIDs
    }

    public static let conservativeAccount = QuotaAppliesTo(kind: .account)
}

public struct CapacityItem: Sendable, Hashable {
    public let recordID: String
    public let providerID: String
    public let accountRef: String
    public let quotaName: String
    public let unit: String
    public let used: String?
    public let limit: String?
    public let remaining: String?
    public let remainingRatio: Double?
    public let resetsAt: String?
    public let periodStart: String?
    public let periodEnd: String?
    public let observedAt: String
    public let freshnessSeconds: Int64
    public let state: String
    public let quality: String
    public let stale: Bool
    public let revision: Int64
    public let sourceID: String
    public let quotaWindow: String
    public let appliesTo: QuotaAppliesTo
    public let providerDescriptor: ProviderDisplayDescriptor

    public init(
        recordID: String, providerID: String, accountRef: String, quotaName: String,
        unit: String, used: String?, limit: String?, remaining: String?,
        remainingRatio: Double?, resetsAt: String?, periodStart: String?, periodEnd: String?,
        observedAt: String, freshnessSeconds: Int64, state: String, quality: String,
        stale: Bool, revision: Int64,
        sourceID: String = "current.quota",
        quotaWindow: String = "subscription",
        appliesTo: QuotaAppliesTo = .conservativeAccount,
        providerDescriptor: ProviderDisplayDescriptor? = nil
    ) {
        self.recordID = recordID
        self.providerID = providerID
        self.accountRef = accountRef
        self.quotaName = quotaName
        self.unit = unit
        self.used = used
        self.limit = limit
        self.remaining = remaining
        self.remainingRatio = remainingRatio
        self.resetsAt = resetsAt
        self.periodStart = periodStart
        self.periodEnd = periodEnd
        self.observedAt = observedAt
        self.freshnessSeconds = freshnessSeconds
        self.state = state
        self.quality = quality
        self.stale = stale
        self.revision = revision
        self.sourceID = sourceID
        self.quotaWindow = quotaWindow
        self.appliesTo = appliesTo
        self.providerDescriptor = providerDescriptor ?? ProviderCatalog.descriptor(for: providerID)
    }
}

public struct SourceHealthItem: Sendable, Hashable {
    public let providerID: String
    public let sourceID: String
    public let state: String
    public let effectiveState: String
    public let lastAttemptAt: String
    public let lastSuccessAt: String?
    public let staleAt: String?
    public let errorCode: String?
}

public struct SourceHealth: Sendable, Hashable {
    public let sources: [SourceHealthItem]
    public let hasIssues: Bool
    public let revision: Int64

    public init(sources: [SourceHealthItem], hasIssues: Bool, revision: Int64) {
        self.sources = sources
        self.hasIssues = hasIssues
        self.revision = revision
    }
}

public struct QuotaHistoryItem: Identifiable, Sendable, Hashable {
    public let snapshotID: Int64
    public let recordID: String
    public let observedAt: String
    public let providerID: String
    public let accountRef: String
    public let quotaName: String
    public let remainingRatio: Double?
    public let resetsAt: String?
    public let state: String
    public let stale: Bool
    public let sourceID: String
    public let quotaWindow: String
    public let appliesTo: QuotaAppliesTo
    public var id: Int64 { snapshotID }
    public var seriesID: String {
        [providerID, accountRef, recordID, quotaName].joined(separator: "|")
    }

    public init(
        snapshotID: Int64, recordID: String, observedAt: String,
        providerID: String, accountRef: String, quotaName: String,
        remainingRatio: Double?, resetsAt: String? = nil,
        state: String, stale: Bool,
        sourceID: String = "current.quota",
        quotaWindow: String = "subscription",
        appliesTo: QuotaAppliesTo = .conservativeAccount
    ) {
        self.snapshotID = snapshotID
        self.recordID = recordID
        self.observedAt = observedAt
        self.providerID = providerID
        self.accountRef = accountRef
        self.quotaName = quotaName
        self.remainingRatio = remainingRatio
        self.resetsAt = resetsAt
        self.state = state
        self.stale = stale
        self.sourceID = sourceID
        self.quotaWindow = quotaWindow
        self.appliesTo = appliesTo
    }
}

public struct QuotaHistoryResult: Sendable, Hashable {
    public let items: [QuotaHistoryItem]
    public let truncatedSeriesIDs: Set<String>
    public let perSeriesLimit: Int
    public var isTruncated: Bool { !truncatedSeriesIDs.isEmpty }

    public init(
        items: [QuotaHistoryItem], truncatedSeriesIDs: Set<String>, perSeriesLimit: Int
    ) {
        self.items = items
        self.truncatedSeriesIDs = truncatedSeriesIDs
        self.perSeriesLimit = perSeriesLimit
    }
}

public struct CompactSummary: Sendable, Hashable {
    /// Nil means no canonical usage or coverage evidence exists for the day.
    /// A non-nil value is the observed total; `isTodayComplete` says whether
    /// every known Provider/account scope is covered.
    public let todayTokens: Int64?
    public let isTodayComplete: Bool
    public let capacity: [CapacityItem]
    public let updatedAt: String?
    public let hasHealthIssues: Bool
    public let revision: Int64
}
