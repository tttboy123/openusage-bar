public struct ProductVersionTruth: Equatable, Sendable {
    public let displayName: String
    public let legacyDisplayName: String
    public let candidateVersion: String
    public let candidateBuild: String
    public let channel: String
    public let releaseStage: String
    public let publicationStatus: String
    public let releaseEligible: Bool
    public let publishedBaselineVersion: String
    public let publishedBaselineTag: String
    public let canaryQualifiedMachines: Int
    public let canaryTargetMachines: Int
    public let canaryClock: String

    public static let current = ProductVersionTruth(
        displayName: "UsageHub",
        legacyDisplayName: "OpenUsage Bar",
        candidateVersion: "0.8.6",
        candidateBuild: "28",
        channel: "rc",
        releaseStage: "candidate",
        publicationStatus: "not_published",
        releaseEligible: false,
        publishedBaselineVersion: "0.7.1",
        publishedBaselineTag: "v0.7.1",
        canaryQualifiedMachines: 0,
        canaryTargetMachines: 5,
        canaryClock: "not_started"
    )

    public var formattedCandidateVersion: String {
        Self.formatCandidateVersion(version: candidateVersion, channel: channel)
    }

    public static func formatCandidateVersion(version: String, channel: String) -> String {
        "\(version) \(channel.uppercased())"
    }
}
