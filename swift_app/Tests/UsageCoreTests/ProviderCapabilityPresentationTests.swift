import Testing
@testable import UsageCore

@Suite("Provider capability presentation")
struct ProviderCapabilityPresentationTests {
    @Test("Presentation values are Sendable and Hashable")
    func domainConformance() {
        assertSendableHashable(ProviderCapabilityItemID.self)
        assertSendableHashable(ProviderCapabilityItem.self)
        assertSendableHashable(ProviderCapabilityGroup.self)
        assertSendableHashable(ProviderCapabilityPresentation.self)
        assertSendableHashable(ProviderSourceStrategyPresentation.self)
        assertSendableHashable(ProviderRuntimeSourcePresentation.self)
    }

    @Test("Known Provider summaries contain only supported capabilities in fixed order")
    func knownSummaries() throws {
        let codex = ProviderCapabilityPresentation(
            descriptor: try #require(GeneratedProviderCatalog.families["codex"])
        )
        #expect(codex.summary == "5-hour + weekly quota · Token history · Model breakdown · Reset dates")

        let kiro = ProviderCapabilityPresentation(
            descriptor: try #require(GeneratedProviderCatalog.families["kiro_cli"])
        )
        #expect(kiro.summary == "Billing-cycle quota · Token history · Model breakdown · Reset dates · Credits · Balance")

        let stepPlan = ProviderCapabilityPresentation(
            descriptor: try #require(GeneratedProviderCatalog.families["step_plan"])
        )
        #expect(stepPlan.summary == "5-hour + weekly quota · Reset dates · Billing · Credits · Balance")

        let openAI = ProviderCapabilityPresentation(
            descriptor: try #require(GeneratedProviderCatalog.families["openai"])
        )
        #expect(openAI.summary == "Token history · Model breakdown · Billing · Cost")
    }

    @Test("Capability details retain Supported Unsupported and Unknown as separate groups")
    func stateGroupsRemainSeparate() throws {
        let presentation = ProviderCapabilityPresentation(
            descriptor: try #require(GeneratedProviderCatalog.families["codex"])
        )

        #expect(presentation.groups.map(\.state) == [.supported, .unsupported, .unknown])
        #expect(presentation.groups.map(\.title) == ["Supported", "Unsupported", "Unknown"])
        #expect(presentation.groups.count == 3)
        #expect(presentation.groups[0].items.map(\.id) == [
            .quotaWindows, .tokenHistory, .modelBreakdown, .resetDates,
        ])
        #expect(presentation.groups[1].items.isEmpty)
        #expect(presentation.groups[2].items.map(\.id) == [
            .billing, .credits, .balance, .cost, .rateLimits, .serviceStatus,
        ])
    }

    @Test("Quota windows use canonical display order independent of input Set order")
    func quotaWindowOrder() {
        let profile = ProviderCapabilityProfile(
            quotaWindows: .supported(
                .modelSpecific, .monthly, .session, .billingCycle, .weekly, .fiveHour
            ),
            tokenHistory: .unknown, modelBreakdown: .unknown,
            resetTimestamps: .unknown, billing: .unknown, credits: .unknown,
            balance: .unknown, cost: .unknown, rateLimits: .unknown,
            serviceStatus: .unknown
        )
        let presentation = ProviderCapabilityPresentation(
            profile: profile, sourceCapabilities: []
        )

        #expect(presentation.summary == "Session + 5-hour + weekly + monthly + billing-cycle + model-specific quota")
        #expect(presentation.groups[0].items.first?.title == presentation.summary)
    }

    @Test("Explicit unsupported facts never masquerade as unclassified")
    func unsupportedSummary() {
        let profile = ProviderCapabilityProfile(
            quotaWindows: .unsupported,
            tokenHistory: .unknown, modelBreakdown: .unknown,
            resetTimestamps: .unknown, billing: .unknown, credits: .unknown,
            balance: .unknown, cost: .unknown, rateLimits: .unknown,
            serviceStatus: .unknown
        )
        let presentation = ProviderCapabilityPresentation(
            profile: profile, sourceCapabilities: []
        )

        #expect(presentation.summary == "No supported capabilities")
        #expect(presentation.groups[1].items.map(\.id) == [.quotaWindows])
        #expect(presentation.groups[2].items.count == 9)
    }

    @Test("Runtime source resolution is exact and handles canonical runtime roles")
    func exactRuntimeSourceResolution() throws {
        let codex = try #require(GeneratedProviderCatalog.families["codex"])
        let exact = ProviderRuntimeSourcePresentation.resolve(
            runtimeSourceID: "codex_local_log", descriptor: codex
        )
        #expect(exact.roleTitle == "Local log")
        #expect(exact.strategies.map(\.summary) == ["Local log · Stable · Provider local"])

        let nearMiss = ProviderRuntimeSourcePresentation.resolve(
            runtimeSourceID: "codex_local_log_extra", descriptor: codex
        )
        #expect(nearMiss.roleTitle == "Uncatalogued source")
        #expect(nearMiss.strategies.isEmpty)

        let daily = ProviderRuntimeSourcePresentation.resolve(
            runtimeSourceID: "openusage.daily", descriptor: codex
        )
        #expect(daily.roleTitle == "Token history")
        #expect(daily.strategies.map(\.summary) == ["OpenUsage · Pinned · OpenUsage upstream"])

        let kiro = try #require(GeneratedProviderCatalog.families["kiro_cli"])
        let quota = ProviderRuntimeSourcePresentation.resolve(
            runtimeSourceID: "current.quota", descriptor: kiro
        )
        #expect(quota.roleTitle == "Current quota")
        #expect(quota.strategies.map(\.summary) == [
            "Keychain · Stable · Provider local",
            "Official API · Stable · Provider official",
        ])
    }

    @Test("Source strategies expose provenance stability and platforms without private identifiers")
    func sourcePrivacyAndPlatforms() {
        let source = ProviderSourceCapability(
            sourceID: "private_account_alice", sourceKind: "browser_session",
            operatingSystems: [.linux, .macOS, .windows], stability: .experimental,
            provenance: .userSession
        )
        let strategy = ProviderSourceStrategyPresentation(source: source)
        let output = [strategy.summary, strategy.platforms].joined(separator: " ")

        #expect(strategy.summary == "Browser session · Experimental · User session")
        #expect(strategy.platforms == "macOS, Windows, Linux")
        #expect(!output.contains("private_account_alice"))
        #expect(!output.contains("credential"))
        #expect(!output.contains("alice"))
    }

    private func assertSendableHashable<T: Sendable & Hashable>(_ type: T.Type) {
        _ = type
    }
}
