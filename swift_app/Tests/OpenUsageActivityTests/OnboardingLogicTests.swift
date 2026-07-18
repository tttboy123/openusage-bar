import Testing
@testable import OpenUsageActivity

@Suite("First-run assessment")
struct OnboardingLogicTests {
    @Test("Existing trusted facts and background launches stay silent")
    func hiddenStates() {
        #expect(FirstRunAssessment.evaluate(
            wasExplicitlyOpened: true, userSkipped: false,
            providerFamilyIDs: ["codex"], configuredFamilyIDs: ["codex"],
            hasTrustworthyFact: true, isRefreshing: false
        ) == .hidden)
        #expect(FirstRunAssessment.evaluate(
            wasExplicitlyOpened: false, userSkipped: false,
            providerFamilyIDs: ["codex"], configuredFamilyIDs: [],
            hasTrustworthyFact: false, isRefreshing: false
        ) == .hidden)
    }

    @Test("Detected local clients are reviewed before adding a connection")
    func detectedProviders() {
        #expect(FirstRunAssessment.evaluate(
            wasExplicitlyOpened: true, userSkipped: false,
            providerFamilyIDs: ["cursor", "codex", "cursor"],
            configuredFamilyIDs: [], hasTrustworthyFact: false,
            isRefreshing: false
        ) == .discoverableProviders(["codex", "cursor"]))
    }

    @Test("A first run without discovery asks for one connection")
    func needsConnection() {
        #expect(FirstRunAssessment.evaluate(
            wasExplicitlyOpened: true, userSkipped: false,
            providerFamilyIDs: [], configuredFamilyIDs: [],
            hasTrustworthyFact: false, isRefreshing: false
        ) == .needsConnection([]))
    }

    @Test("Configured empty sources and active refresh enter collecting")
    func collecting() {
        #expect(FirstRunAssessment.evaluate(
            wasExplicitlyOpened: true, userSkipped: false,
            providerFamilyIDs: [], configuredFamilyIDs: ["minimax"],
            hasTrustworthyFact: false, isRefreshing: false
        ) == .collecting)
        #expect(FirstRunAssessment.evaluate(
            wasExplicitlyOpened: true, userSkipped: false,
            providerFamilyIDs: [], configuredFamilyIDs: [],
            hasTrustworthyFact: false, isRefreshing: true
        ) == .collecting)
    }

    @Test("Skip suppresses automatic onboarding without erasing the manual entry")
    func skip() {
        #expect(FirstRunAssessment.evaluate(
            wasExplicitlyOpened: true, userSkipped: true,
            providerFamilyIDs: ["codex"], configuredFamilyIDs: [],
            hasTrustworthyFact: false, isRefreshing: false
        ) == .hidden)
        #expect(FirstRunAssessment.manualEntryTitleKey == "Getting Started")
    }
}
