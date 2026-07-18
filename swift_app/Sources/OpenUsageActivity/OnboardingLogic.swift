import Foundation

enum FirstRunPhase: Sendable, Hashable {
    case hidden
    case discoverableProviders([String])
    case needsConnection([String])
    case collecting
    case ready
}

enum FirstRunAssessment {
    static let manualEntryTitleKey = "Getting Started"

    static func evaluate(
        wasExplicitlyOpened: Bool,
        userSkipped: Bool,
        providerFamilyIDs: [String],
        configuredFamilyIDs: [String],
        hasTrustworthyFact: Bool,
        isRefreshing: Bool
    ) -> FirstRunPhase {
        guard wasExplicitlyOpened, !userSkipped, !hasTrustworthyFact else {
            return .hidden
        }
        let discovered = Array(Set(providerFamilyIDs).subtracting(configuredFamilyIDs)).sorted()
        if !discovered.isEmpty { return .discoverableProviders(discovered) }
        if isRefreshing || !configuredFamilyIDs.isEmpty { return .collecting }
        return .needsConnection([])
    }
}

enum OnboardingRouteMessage {
    static let notification = Notification.Name("com.openusage.bar.onboarding.request")
}

extension ActivityLoadedData {
    var hasTrustworthyFact: Bool {
        details.heatmapDays.contains { $0.state != .missing }
            || capacity.contains { $0.remainingRatio != nil || $0.remaining != nil }
            || apiSpend.coverage != .missing
    }

    var detectedLocalFamilyIDs: [String] {
        let localSignals = ["local", "cli", "oauth", "keychain", "auto"]
        return providerInstances.filter { instance in
            localSignals.contains { instance.credentialSource.lowercased().contains($0) }
        }.map(\.familyID)
    }

    var configuredFamilyIDs: [String] {
        let detected = Set(detectedLocalFamilyIDs)
        return providerInstances.map(\.familyID).filter { !detected.contains($0) }
    }
}
