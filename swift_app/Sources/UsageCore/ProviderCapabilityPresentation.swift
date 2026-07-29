public enum ProviderCapabilityItemID: String, CaseIterable, Sendable, Hashable {
    case quotaWindows
    case tokenHistory
    case modelBreakdown
    case resetDates
    case billing
    case credits
    case balance
    case cost
    case rateLimits
    case serviceStatus
}

public struct ProviderCapabilityItem: Sendable, Hashable {
    public let id: ProviderCapabilityItemID
    public let title: String
    public let state: ProviderCapabilityState
}

public struct ProviderCapabilityGroup: Sendable, Hashable {
    public let state: ProviderCapabilityState
    public let title: String
    public let items: [ProviderCapabilityItem]
}

public struct ProviderCapabilityPresentation: Sendable, Hashable {
    public let summary: String
    public let groups: [ProviderCapabilityGroup]
    public let sourceStrategies: [ProviderSourceStrategyPresentation]

    public init(descriptor: ProviderDisplayDescriptor) {
        self.init(
            profile: descriptor.capabilityProfile,
            sourceCapabilities: descriptor.sourceCapabilities
        )
    }

    public init(
        profile: ProviderCapabilityProfile,
        sourceCapabilities: [ProviderSourceCapability]
    ) {
        let items = Self.items(profile)
        let supported = items.filter { $0.state == .supported }
        if !supported.isEmpty {
            summary = supported.map(\.title).joined(separator: " · ")
        } else if items.allSatisfy({ $0.state == .unknown }) {
            summary = AppLocalization.text("Capabilities not yet classified")
        } else {
            summary = AppLocalization.text("No supported capabilities")
        }
        groups = [
            Self.group(.supported, items: items),
            Self.group(.unsupported, items: items),
            Self.group(.unknown, items: items),
        ]
        sourceStrategies = sourceCapabilities.map(ProviderSourceStrategyPresentation.init)
    }

    private static func items(_ profile: ProviderCapabilityProfile) -> [ProviderCapabilityItem] {
        [
            ProviderCapabilityItem(
                id: .quotaWindows,
                title: quotaTitle(profile.quotaWindows),
                state: profile.quotaWindows.state
            ),
            ProviderCapabilityItem(
                id: .tokenHistory, title: AppLocalization.text("Token history"), state: profile.tokenHistory
            ),
            ProviderCapabilityItem(
                id: .modelBreakdown, title: AppLocalization.text("Model breakdown"), state: profile.modelBreakdown
            ),
            ProviderCapabilityItem(
                id: .resetDates, title: AppLocalization.text("Reset dates"), state: profile.resetTimestamps
            ),
            ProviderCapabilityItem(id: .billing, title: AppLocalization.text("Billing"), state: profile.billing),
            ProviderCapabilityItem(id: .credits, title: AppLocalization.text("Credits"), state: profile.credits),
            ProviderCapabilityItem(id: .balance, title: AppLocalization.text("Balance"), state: profile.balance),
            ProviderCapabilityItem(id: .cost, title: AppLocalization.text("Cost"), state: profile.cost),
            ProviderCapabilityItem(
                id: .rateLimits, title: AppLocalization.text("Rate limits"), state: profile.rateLimits
            ),
            ProviderCapabilityItem(
                id: .serviceStatus, title: AppLocalization.text("Service status"), state: profile.serviceStatus
            ),
        ]
    }

    private static func group(
        _ state: ProviderCapabilityState, items: [ProviderCapabilityItem]
    ) -> ProviderCapabilityGroup {
        let key = switch state {
        case .supported: "Supported"
        case .unsupported: "Unsupported"
        case .unknown: "Unknown"
        }
        let title = AppLocalization.text(key)
        return ProviderCapabilityGroup(
            state: state, title: title, items: items.filter { $0.state == state }
        )
    }

    private static func quotaTitle(_ capability: ProviderQuotaWindowCapability) -> String {
        guard capability.state == .supported else { return AppLocalization.text("Quota windows") }
        let values = ProviderQuotaWindow.allCases.compactMap { window in
            capability.values.contains(window) ? quotaWindowTitle(window) : nil
        }
        let localized = AppLocalization.format("%@ quota", values.joined(separator: " + "))
        return localized.prefix(1).uppercased() + localized.dropFirst()
    }

    private static func quotaWindowTitle(_ window: ProviderQuotaWindow) -> String {
        switch window {
        case .session: AppLocalization.text("session")
        case .fiveHour: "5-hour"
        case .weekly: AppLocalization.text("weekly")
        case .monthly: AppLocalization.text("monthly")
        case .billingCycle: AppLocalization.text("billing-cycle")
        case .modelSpecific: AppLocalization.text("model-specific")
        }
    }
}

public struct ProviderSourceStrategyPresentation: Sendable, Hashable {
    public let kindTitle: String
    public let summary: String
    public let factSummary: String
    public let trustSummary: String
    public let scopeSummary: String
    public let platforms: String

    public init(source: ProviderSourceCapability) {
        kindTitle = Self.kindTitle(source.sourceKind)
        summary = [
            kindTitle,
            Self.stabilityTitle(source.stability),
            Self.provenanceTitle(source.provenance),
        ].joined(separator: " · ")
        factSummary = Self.factSummary(source.factFamilies)
        trustSummary = [
            Self.authorityTitle(source.authority),
            Self.verificationTitle(source.verification),
        ].joined(separator: " · ")
        scopeSummary = [
            Self.accountScopeTitle(source.accountScope),
            Self.modelScopeTitle(source.modelScope),
        ].joined(separator: " · ")
        platforms = Self.platformsTitle(source.operatingSystems)
    }

    private static func kindTitle(_ kind: String) -> String {
        switch kind {
        case "openusage": "OpenUsage"
        case "builtin_api": AppLocalization.text("Built-in API")
        case "official_api": AppLocalization.text("Official API")
        case "cli": "CLI"
        case "local_log": AppLocalization.text("Local log")
        case "local_database": AppLocalization.text("Local database")
        case "keychain": AppLocalization.text("Keychain")
        case "browser_session": AppLocalization.text("Browser session")
        default: AppLocalization.text("Provider source")
        }
    }

    private static func stabilityTitle(_ stability: ProviderSourceStability) -> String {
        switch stability {
        case .stable: AppLocalization.text("Stable")
        case .experimental: AppLocalization.text("Experimental")
        case .pinned: AppLocalization.text("Pinned")
        case .opaque: AppLocalization.text("Opaque")
        }
    }

    private static func provenanceTitle(_ provenance: ProviderSourceProvenance) -> String {
        switch provenance {
        case .openUsageUpstream: AppLocalization.text("OpenUsage upstream")
        case .openUsageBarBuiltIn: AppLocalization.text("OpenUsage Bar built-in")
        case .providerOfficial: AppLocalization.text("Provider official")
        case .providerLocal: AppLocalization.text("Provider local")
        case .userSession: AppLocalization.text("User session")
        }
    }

    private static func factSummary(
        _ facts: Set<ProviderSourceFactFamily>
    ) -> String {
        let values = ProviderSourceFactFamily.allCases.compactMap { fact -> String? in
            guard facts.contains(fact) else { return nil }
            return switch fact {
            case .detection: AppLocalization.text("Detection")
            case .tokenActivity: AppLocalization.text("Token activity")
            case .subscriptionCapacity: AppLocalization.text("Subscription capacity")
            case .apiSpend: AppLocalization.text("API spend")
            }
        }
        return values.isEmpty
            ? AppLocalization.text("No declared facts")
            : values.joined(separator: " · ")
    }

    private static func authorityTitle(_ authority: ProviderSourceAuthority) -> String {
        switch authority {
        case .providerOfficial: AppLocalization.text("Provider official data")
        case .providerLocal: AppLocalization.text("Provider local data")
        case .thirdParty: AppLocalization.text("Third-party derived")
        case .userSupplied: AppLocalization.text("User supplied")
        case .unknown: AppLocalization.text("Authority unknown")
        }
    }

    private static func verificationTitle(
        _ verification: ProviderSourceVerification
    ) -> String {
        switch verification {
        case .liveAccount: AppLocalization.text("Real account verified")
        case .fixture: AppLocalization.text("Fixture verified")
        case .upstreamDeclared: AppLocalization.text("Upstream declared")
        case .unverified: AppLocalization.text("Unverified")
        }
    }

    private static func accountScopeTitle(
        _ scope: ProviderSourceAccountScope
    ) -> String {
        switch scope {
        case .localProfile: AppLocalization.text("Local profile")
        case .configuredAccount: AppLocalization.text("Configured account")
        case .organization: AppLocalization.text("Organization")
        case .provider: AppLocalization.text("Provider aggregate")
        case .unknown: AppLocalization.text("Account scope unknown")
        }
    }

    private static func modelScopeTitle(_ scope: ProviderSourceModelScope) -> String {
        switch scope {
        case .perModel: AppLocalization.text("Per model")
        case .aggregate: AppLocalization.text("Aggregate only")
        case .mixed: AppLocalization.text("Mixed model scope")
        case .unknown: AppLocalization.text("Model scope unknown")
        }
    }

    fileprivate static func platformsTitle(
        _ operatingSystems: Set<ProviderSourceOperatingSystem>
    ) -> String {
        ProviderSourceOperatingSystem.allCases.compactMap { system in
            guard operatingSystems.contains(system) else { return nil }
            return switch system {
            case .macOS: "macOS"
            case .windows: "Windows"
            case .linux: "Linux"
            }
        }.joined(separator: ", ")
    }
}

public struct ProviderRuntimeSourcePresentation: Sendable, Hashable {
    public let roleTitle: String
    public let strategies: [ProviderSourceStrategyPresentation]
    public let platforms: String

    public static func resolve(
        runtimeSourceID: String, descriptor: ProviderDisplayDescriptor
    ) -> Self {
        if let exact = descriptor.sourceCapabilities.first(where: {
            $0.sourceID == runtimeSourceID
        }) {
            return make(
                roleTitle: ProviderSourceStrategyPresentation(source: exact).kindTitle,
                sources: [exact]
            )
        }
        if runtimeSourceID == "openusage.daily" {
            return make(
                roleTitle: AppLocalization.text("Token history"),
                sources: descriptor.sourceCapabilities.filter { $0.sourceKind == "openusage" }
            )
        }
        if runtimeSourceID == "current.quota" {
            return make(
                roleTitle: AppLocalization.text("Current quota"),
                sources: descriptor.sourceCapabilities.filter { $0.sourceKind != "openusage" }
            )
        }
        return uncatalogued
    }

    private static func make(
        roleTitle: String, sources: [ProviderSourceCapability]
    ) -> Self {
        guard !sources.isEmpty else { return uncatalogued }
        let systems = sources.reduce(into: Set<ProviderSourceOperatingSystem>()) {
            $0.formUnion($1.operatingSystems)
        }
        return Self(
            roleTitle: roleTitle,
            strategies: sources.map(ProviderSourceStrategyPresentation.init),
            platforms: ProviderSourceStrategyPresentation.platformsTitle(systems)
        )
    }

    private static let uncatalogued = Self(
        roleTitle: AppLocalization.text("Uncatalogued source"), strategies: [],
        platforms: AppLocalization.text("Unavailable")
    )
}
