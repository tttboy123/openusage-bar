import Testing
import UsageCore

@Suite("Product version truth projection")
struct ProductVersionTruthTests {
    @Test("Exposes only the public build identity facts")
    func exposesBuildIdentity() {
        let identity = ProductVersionTruth.current

        #expect(identity.displayName == "UsageHub")
        #expect(identity.legacyDisplayName == "OpenUsage Bar")
        #expect(identity.candidateVersion == "0.8.6")
        #expect(identity.candidateBuild == "28")
        #expect(identity.channel == "rc")
        #expect(identity.releaseStage == "candidate")
        #expect(identity.publicationStatus == "not_published")
        #expect(identity.releaseEligible == false)
        #expect(identity.publishedBaselineVersion == "0.7.1")
        #expect(identity.publishedBaselineTag == "v0.7.1")
        #expect(identity.canaryQualifiedMachines == 0)
        #expect(identity.canaryTargetMachines == 5)
        #expect(identity.canaryClock == "not_started")
    }

    @Test("Formats version and channel without embedding final UI copy")
    func formatsCandidateVersion() {
        #expect(ProductVersionTruth.current.formattedCandidateVersion == "0.8.6 RC")
        #expect(
            ProductVersionTruth.formatCandidateVersion(version: "1.2.3", channel: "beta")
                == "1.2.3 BETA"
        )
    }
}
