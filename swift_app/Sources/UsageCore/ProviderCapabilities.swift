public enum ProviderCapabilityState: String, CaseIterable, Sendable, Hashable {
    case supported
    case unsupported
    case unknown
}

public enum ProviderQuotaWindow: String, CaseIterable, Sendable, Hashable {
    case session
    case fiveHour = "five_hour"
    case weekly
    case monthly
    case billingCycle = "billing_cycle"
    case modelSpecific = "model_specific"
}

public struct ProviderQuotaWindowCapability: Sendable, Hashable {
    public let state: ProviderCapabilityState
    public let values: Set<ProviderQuotaWindow>

    public init?(state: ProviderCapabilityState, values: Set<ProviderQuotaWindow>) {
        switch state {
        case .supported:
            guard !values.isEmpty else { return nil }
        case .unknown, .unsupported:
            guard values.isEmpty else { return nil }
        }
        self.state = state
        self.values = values
    }

    private init(validatedState: ProviderCapabilityState, values: Set<ProviderQuotaWindow>) {
        self.state = validatedState
        self.values = values
    }

    public static func supported(
        _ first: ProviderQuotaWindow,
        _ additional: ProviderQuotaWindow...
    ) -> ProviderQuotaWindowCapability {
        ProviderQuotaWindowCapability(
            validatedState: .supported,
            values: Set([first] + additional)
        )
    }

    public static let unknown = ProviderQuotaWindowCapability(
        validatedState: .unknown,
        values: []
    )

    public static let unsupported = ProviderQuotaWindowCapability(
        validatedState: .unsupported,
        values: []
    )
}

public struct ProviderCapabilityProfile: Sendable, Hashable {
    public let quotaWindows: ProviderQuotaWindowCapability
    public let tokenHistory: ProviderCapabilityState
    public let modelBreakdown: ProviderCapabilityState
    public let resetTimestamps: ProviderCapabilityState
    public let billing: ProviderCapabilityState
    public let credits: ProviderCapabilityState
    public let balance: ProviderCapabilityState
    public let cost: ProviderCapabilityState
    public let rateLimits: ProviderCapabilityState
    public let serviceStatus: ProviderCapabilityState

    public init(
        quotaWindows: ProviderQuotaWindowCapability,
        tokenHistory: ProviderCapabilityState,
        modelBreakdown: ProviderCapabilityState,
        resetTimestamps: ProviderCapabilityState,
        billing: ProviderCapabilityState,
        credits: ProviderCapabilityState,
        balance: ProviderCapabilityState,
        cost: ProviderCapabilityState,
        rateLimits: ProviderCapabilityState,
        serviceStatus: ProviderCapabilityState
    ) {
        self.quotaWindows = quotaWindows
        self.tokenHistory = tokenHistory
        self.modelBreakdown = modelBreakdown
        self.resetTimestamps = resetTimestamps
        self.billing = billing
        self.credits = credits
        self.balance = balance
        self.cost = cost
        self.rateLimits = rateLimits
        self.serviceStatus = serviceStatus
    }

    public static let unknown = ProviderCapabilityProfile(
        quotaWindows: .unknown,
        tokenHistory: .unknown,
        modelBreakdown: .unknown,
        resetTimestamps: .unknown,
        billing: .unknown,
        credits: .unknown,
        balance: .unknown,
        cost: .unknown,
        rateLimits: .unknown,
        serviceStatus: .unknown
    )
}

public enum ProviderSourceOperatingSystem: String, CaseIterable, Sendable, Hashable {
    case macOS = "macos"
    case windows
    case linux
}

public enum ProviderSourceStability: String, CaseIterable, Sendable, Hashable {
    case stable
    case experimental
    case pinned
    case opaque
}

public enum ProviderSourceProvenance: String, CaseIterable, Sendable, Hashable {
    case openUsageUpstream = "openusage_upstream"
    case openUsageBarBuiltIn = "openusage_bar_builtin"
    case providerOfficial = "provider_official"
    case providerLocal = "provider_local"
    case userSession = "user_session"
}

public struct ProviderSourceCapability: Sendable, Hashable {
    public let sourceID: String
    public let sourceKind: String
    public let operatingSystems: Set<ProviderSourceOperatingSystem>
    public let stability: ProviderSourceStability
    public let provenance: ProviderSourceProvenance

    public init(
        sourceID: String,
        sourceKind: String,
        operatingSystems: Set<ProviderSourceOperatingSystem>,
        stability: ProviderSourceStability,
        provenance: ProviderSourceProvenance
    ) {
        self.sourceID = sourceID
        self.sourceKind = sourceKind
        self.operatingSystems = operatingSystems
        self.stability = stability
        self.provenance = provenance
    }

    public static let openUsageFallback = ProviderSourceCapability(
        sourceID: "openusage",
        sourceKind: "openusage",
        operatingSystems: [.macOS],
        stability: .pinned,
        provenance: .openUsageUpstream
    )
}
