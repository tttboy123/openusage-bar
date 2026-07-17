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
            summary = "Capabilities not yet classified"
        } else {
            summary = "No supported capabilities"
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
                id: .tokenHistory, title: "Token history", state: profile.tokenHistory
            ),
            ProviderCapabilityItem(
                id: .modelBreakdown, title: "Model breakdown", state: profile.modelBreakdown
            ),
            ProviderCapabilityItem(
                id: .resetDates, title: "Reset dates", state: profile.resetTimestamps
            ),
            ProviderCapabilityItem(id: .billing, title: "Billing", state: profile.billing),
            ProviderCapabilityItem(id: .credits, title: "Credits", state: profile.credits),
            ProviderCapabilityItem(id: .balance, title: "Balance", state: profile.balance),
            ProviderCapabilityItem(id: .cost, title: "Cost", state: profile.cost),
            ProviderCapabilityItem(
                id: .rateLimits, title: "Rate limits", state: profile.rateLimits
            ),
            ProviderCapabilityItem(
                id: .serviceStatus, title: "Service status", state: profile.serviceStatus
            ),
        ]
    }

    private static func group(
        _ state: ProviderCapabilityState, items: [ProviderCapabilityItem]
    ) -> ProviderCapabilityGroup {
        let title = switch state {
        case .supported: "Supported"
        case .unsupported: "Unsupported"
        case .unknown: "Unknown"
        }
        return ProviderCapabilityGroup(
            state: state, title: title, items: items.filter { $0.state == state }
        )
    }

    private static func quotaTitle(_ capability: ProviderQuotaWindowCapability) -> String {
        guard capability.state == .supported else { return "Quota windows" }
        let values = ProviderQuotaWindow.allCases.compactMap { window in
            capability.values.contains(window) ? quotaWindowTitle(window) : nil
        }
        let raw = values.joined(separator: " + ") + " quota"
        return raw.prefix(1).uppercased() + raw.dropFirst()
    }

    private static func quotaWindowTitle(_ window: ProviderQuotaWindow) -> String {
        switch window {
        case .session: "session"
        case .fiveHour: "5-hour"
        case .weekly: "weekly"
        case .monthly: "monthly"
        case .billingCycle: "billing-cycle"
        case .modelSpecific: "model-specific"
        }
    }
}

public struct ProviderSourceStrategyPresentation: Sendable, Hashable {
    public let kindTitle: String
    public let summary: String
    public let platforms: String

    public init(source: ProviderSourceCapability) {
        kindTitle = Self.kindTitle(source.sourceKind)
        summary = [
            kindTitle,
            Self.stabilityTitle(source.stability),
            Self.provenanceTitle(source.provenance),
        ].joined(separator: " · ")
        platforms = Self.platformsTitle(source.operatingSystems)
    }

    private static func kindTitle(_ kind: String) -> String {
        switch kind {
        case "openusage": "OpenUsage"
        case "builtin_api": "Built-in API"
        case "official_api": "Official API"
        case "cli": "CLI"
        case "local_log": "Local log"
        case "local_database": "Local database"
        case "keychain": "Keychain"
        case "browser_session": "Browser session"
        default: "Provider source"
        }
    }

    private static func stabilityTitle(_ stability: ProviderSourceStability) -> String {
        switch stability {
        case .stable: "Stable"
        case .experimental: "Experimental"
        case .pinned: "Pinned"
        case .opaque: "Opaque"
        }
    }

    private static func provenanceTitle(_ provenance: ProviderSourceProvenance) -> String {
        switch provenance {
        case .openUsageUpstream: "OpenUsage upstream"
        case .openUsageBarBuiltIn: "OpenUsage Bar built-in"
        case .providerOfficial: "Provider official"
        case .providerLocal: "Provider local"
        case .userSession: "User session"
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
                roleTitle: "Token history",
                sources: descriptor.sourceCapabilities.filter { $0.sourceKind == "openusage" }
            )
        }
        if runtimeSourceID == "current.quota" {
            return make(
                roleTitle: "Current quota",
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
        roleTitle: "Uncatalogued source", strategies: [], platforms: "Unavailable"
    )
}
