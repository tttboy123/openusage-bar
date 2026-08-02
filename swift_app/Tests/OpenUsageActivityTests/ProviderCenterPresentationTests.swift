import Foundation
import Testing
@testable import UsageCore
@testable import OpenUsageActivity

@Suite("Provider Center presentation")
struct ProviderCenterPresentationTests {
    @Test("Managed drafts encode strict v2 create update and remove envelopes")
    func mutationV2Envelopes() throws {
        let draft = ManagedConnectionDraft.minimax(
            providerID: "minimax-work", name: "MiniMax Work",
            site: "international",
            replacementCredential: "private-key"
        )
        let create = try mutationObject(draft.request(action: .createConnection))
        #expect(create["version"] as? Int == 2)
        #expect(create["action"] as? String == "create_connection")
        #expect(create["kind"] as? String == "minimax")
        #expect((create["configuration"] as? [String: Any])?["name"] as? String == "MiniMax Work")
        #expect((create["configuration"] as? [String: Any])?["site"] as? String == "international")
        #expect((create["credentialMaterial"] as? [String: Any])?["primary"] as? String == "private-key")

        let remove = try mutationObject(draft.request(action: .removeConnection))
        #expect((remove["configuration"] as? [String: Any])?.isEmpty == true)
        #expect((remove["credentialMaterial"] as? [String: Any])?.isEmpty == true)
    }

    @Test("Draft validation keeps credentials transient and rejects incomplete forms")
    func draftValidation() {
        let invalid = ManagedConnectionDraft.stepPlan(
            providerID: "step-work", name: "", site: "china",
            replacementCredential: "", replacementSession: ""
        )
        #expect(invalid.validation(action: .createConnection) == .missingName)

        let missingCredential = ManagedConnectionDraft.minimax(
            providerID: "minimax-work", name: "MiniMax",
            site: "china",
            replacementCredential: ""
        )
        #expect(missingCredential.validation(action: .createConnection) == .missingCredential)
        #expect(missingCredential.validation(action: .updateConnection) == nil)

        let missingProvider = ManagedConnectionDraft.minimax(
            providerID: "  ", name: "MiniMax", site: "china",
            replacementCredential: "key"
        )
        #expect(missingProvider.validation(action: .createConnection) == .missingProviderID)

        let invalidSite = ManagedConnectionDraft.stepPlan(
            providerID: "step-work", name: "Step Plan", site: "elsewhere",
            replacementCredential: "key", replacementSession: ""
        )
        #expect(invalidSite.validation(action: .createConnection) == .invalidSite)

        let invalidMiniMaxSite = ManagedConnectionDraft.minimax(
            providerID: "minimax-work", name: "MiniMax", site: "elsewhere",
            replacementCredential: "key"
        )
        #expect(invalidMiniMaxSite.validation(action: .createConnection) == .invalidSite)

        let invalidMoonshotSite = ManagedConnectionDraft.moonshot(
            providerID: "moonshot-work", name: "Kimi", site: "elsewhere",
            replacementCredential: "key"
        )
        #expect(invalidMoonshotSite.validation(action: .createConnection) == .invalidSite)

        let sessionOnly = ManagedConnectionDraft.stepPlan(
            providerID: "step-work", name: "Step Plan", site: "china",
            replacementCredential: "", replacementSession: "session"
        )
        #expect(sessionOnly.validation(action: .createConnection) == nil)
    }

    @Test("Every managed Provider draft serializes its public configuration")
    func allManagedDraftEnvelopes() throws {
        let moonshot = ManagedConnectionDraft.moonshot(
            providerID: "moonshot-work", name: "Kimi Work", site: "china",
            replacementCredential: "moonshot-key"
        )
        let step = ManagedConnectionDraft.stepPlan(
            providerID: "step-work", name: "Step Work", site: "international",
            replacementCredential: "api-key", replacementSession: "web-session"
        )
        let organization = ManagedConnectionDraft.openAIOrganization(
            providerID: "openai-work", name: "OpenAI Work",
            replacementCredential: "admin-key"
        )
        let generic = ManagedConnectionDraft.generic(.init(
            providerID: "custom-quota", name: "Custom Quota", familyID: "custom",
            endpoint: "https://example.test/quota", headerName: "Authorization",
            authPrefix: "Bearer ", primaryPath: "$.quota",
            remainingPercentPath: "$.remaining", resetPath: "$.reset",
            detailPath: "$.detail", replacementCredential: "quota-key"
        ))
        let daily = ManagedConnectionDraft.dailyUsageFeed(.init(
            providerID: "custom-daily", name: "Custom Daily", familyID: "custom",
            endpoint: "https://example.test/usage", headerName: "X-API-Key",
            authPrefix: "", itemsPath: "$.items", datePath: "$.date",
            modelPath: "$.model", inputTokensPath: "$.input",
            outputTokensPath: "$.output", cacheReadTokensPath: "$.cacheRead",
            cacheCreationTokensPath: "$.cacheCreate", reasoningTokensPath: "$.reasoning",
            totalTokensPath: "$.total", sinceParameter: "since",
            untilParameter: "until", replacementCredential: "usage-key"
        ))

        let moonshotObject = try mutationObject(
            moonshot.request(action: .createConnection)
        )
        #expect(moonshotObject["kind"] as? String == "moonshot")
        #expect(
            (moonshotObject["configuration"] as? [String: Any])?["site"] as? String
                == "china"
        )
        #expect(
            (moonshotObject["credentialMaterial"] as? [String: Any])?["primary"]
                as? String == "moonshot-key"
        )

        let stepObject = try mutationObject(step.request(action: .createConnection))
        #expect(stepObject["kind"] as? String == "step_plan")
        #expect((stepObject["configuration"] as? [String: Any])?["site"] as? String == "international")
        #expect((stepObject["credentialMaterial"] as? [String: Any])?["session"] as? String == "web-session")

        let organizationObject = try mutationObject(organization.request(action: .updateConnection))
        #expect(organizationObject["kind"] as? String == "openai_organization")

        let genericObject = try mutationObject(generic.request(action: .createConnection))
        let genericConfiguration = try #require(genericObject["configuration"] as? [String: Any])
        #expect(genericObject["kind"] as? String == "generic")
        #expect(genericConfiguration["remainingPercentPath"] as? String == "$.remaining")
        #expect(genericConfiguration["detailPath"] as? String == "$.detail")

        let dailyObject = try mutationObject(daily.request(action: .createConnection))
        let dailyConfiguration = try #require(dailyObject["configuration"] as? [String: Any])
        #expect(dailyObject["kind"] as? String == "daily_usage_feed")
        #expect(dailyConfiguration["modelPath"] as? String == "$.model")
        #expect(dailyConfiguration["reasoningTokensPath"] as? String == "$.reasoning")
        #expect(dailyConfiguration["untilParameter"] as? String == "until")
    }

    @Test("Auto-discovered connections never receive mutation actions")
    func readOnlyDiscovery() {
        #expect(!ProviderCenterPresentation.canMutate(kind: "codex"))
        #expect(!ProviderCenterPresentation.canMutate(kind: "cursor"))
        #expect(ProviderCenterPresentation.canMutate(kind: "minimax"))
        #expect(ProviderCenterPresentation.canMutate(kind: "moonshot"))
        #expect(ProviderCenterPresentation.canMutate(kind: "daily_usage_feed"))
    }

    @Test("Add Provider catalog exposes every family in the correct setup path")
    func addProviderCatalogCoverage() {
        let catalog = ProviderAddCatalog(descriptors: ProviderCatalog.allDescriptors)
        let catalogIDs = Set(ProviderCatalog.allDescriptors.map(\.familyID))
        let presentedIDs = Set(
            catalog.serviceOptions.map(\.descriptor.familyID)
                + catalog.automaticDescriptors.map(\.familyID)
        )

        #expect(presentedIDs == catalogIDs)
        #expect(catalog.serviceOptions.allSatisfy { option in
            option.descriptor.category == .api || option.connectionKind.isBuiltIn
        })
        #expect(catalog.automaticDescriptors.allSatisfy { descriptor in
            descriptor.category != .api
                && ProviderAddConnectionKind(familyID: descriptor.familyID) == nil
        })
    }

    @Test("Add Provider catalog distinguishes native and custom connections")
    func addProviderConnectionKinds() {
        let catalog = ProviderAddCatalog(descriptors: ProviderCatalog.allDescriptors)
        let native = Dictionary(uniqueKeysWithValues: catalog.serviceOptions.compactMap { option in
            option.connectionKind.isBuiltIn
                ? (option.descriptor.familyID, option.connectionKind)
                : nil
        })

        #expect(native == [
            "minimax": .minimax,
            "moonshot": .moonshot,
            "openai": .openAIOrganization,
            "step_plan": .stepPlan,
        ])
        #expect(catalog.customOptions.map(\.connectionKind) == [.generic, .dailyUsageFeed])
        #expect(catalog.customOptions.map(\.id) == ["custom-provider", "custom-daily-usage"])
        #expect(catalog.serviceOptions.first { $0.descriptor.familyID == "anthropic" }?.connectionKind == .generic)
    }

    @Test("Add Provider search covers names aliases identifiers and custom entries")
    func addProviderSearch() {
        let catalog = ProviderAddCatalog(descriptors: ProviderCatalog.allDescriptors)

        #expect(catalog.filteredServiceOptions(query: "Moonshot").map(\.descriptor.familyID) == ["moonshot"])
        #expect(catalog.filteredServiceOptions(query: "智谱").map(\.descriptor.familyID).contains("zai"))
        #expect(catalog.filteredCustomOptions(query: "custom").count == 2)
        #expect(catalog.filteredCustomOptions(query: "daily").map(\.id) == ["custom-daily-usage"])
        #expect(catalog.filteredAutomaticDescriptors(query: "codex").map(\.familyID) == ["codex"])
    }
    @Test("Browse categories separate cloud services from API providers")
    func categories() throws {
        #expect(ProviderBrowseCategory.classify(try descriptor("minimax")) == .subscription)
        #expect(ProviderBrowseCategory.classify(try descriptor("alibaba_cloud")) == .cloud)
        #expect(ProviderBrowseCategory.classify(try descriptor("azure_openai")) == .cloud)
        #expect(ProviderBrowseCategory.classify(try descriptor("deepseek")) == .api)
        #expect(ProviderBrowseCategory.classify(try descriptor("openclaw")) == .local)
    }

    @Test("Site labels preserve China and international separation")
    func siteLabels() throws {
        #expect(ProviderCenterText.scope(try descriptor("minimax")) == "China and International")
        #expect(ProviderCenterText.scope(try descriptor("step_plan")) == "China and International")
        #expect(ProviderCenterText.scope(try descriptor("deepseek")) == nil)
    }

    @Test("Connection methods describe declared credential sources")
    func connectionMethods() throws {
        #expect(ProviderCenterText.connectionMethod(try descriptor("minimax")) == "API Key")
        #expect(ProviderCenterText.connectionMethod(try descriptor("step_plan")) == "API Key or web session")
        #expect(ProviderCenterText.connectionMethod(try descriptor("codex")) == "Existing local login")
        #expect(ProviderCenterText.connectionMethod(try descriptor("openrouter")) == "OpenUsage data source")
    }

    @Test("Filtering searches display names and family identifiers")
    func filtering() throws {
        let items = [
            ProviderCenterItem(
                descriptor: try descriptor("minimax"), instanceCount: 1,
                observed: true, issues: []
            ),
            ProviderCenterItem(
                descriptor: try descriptor("alibaba_cloud"), instanceCount: 0,
                observed: false, issues: []
            ),
            ProviderCenterItem(
                descriptor: try descriptor("openclaw"), instanceCount: 0,
                observed: true, issues: [issue(errorCode: "auth_required")]
            ),
        ]
        #expect(ProviderCenterPresentation.filter(items, category: .all, query: "Mini").map(\.id) == ["minimax"])
        #expect(ProviderCenterPresentation.filter(items, category: .cloud, query: "").map(\.id) == ["alibaba_cloud"])
        #expect(ProviderCenterPresentation.filter(items, category: .all, query: "openclaw").map(\.id) == ["openclaw"])
    }

    @Test("Selection follows visible order without a hard-coded Provider")
    func selection() {
        let visibleIDs = ["codex", "deepseek", "minimax"]
        #expect(ProviderCenterPresentation.selection(current: nil, visibleIDs: visibleIDs) == "codex")
        #expect(ProviderCenterPresentation.selection(current: "deepseek", visibleIDs: visibleIDs) == "deepseek")
        #expect(ProviderCenterPresentation.selection(current: "hidden", visibleIDs: visibleIDs) == "codex")
        #expect(ProviderCenterPresentation.selection(current: "minimax", visibleIDs: []) == nil)
    }

    @Test("Status distinguishes available, connected, and attention")
    func status() throws {
        #expect(ProviderCenterItem(
            descriptor: try descriptor("deepseek"), instanceCount: 0,
            observed: false, issues: []
        ).status == .available)
        #expect(ProviderCenterItem(
            descriptor: try descriptor("minimax"), instanceCount: 1,
            observed: true, issues: []
        ).status == .connected)
        #expect(ProviderCenterItem(
            descriptor: try descriptor("openclaw"), instanceCount: 0,
            observed: true, issues: [issue(errorCode: "auth_required")]
        ).status == .attention)
    }

    @Test("Keychain failures require repair and use the quota label")
    func keychainIssuesRequireRepair() throws {
        let keychain = issue(
            sourceID: "step_plan.quota",
            effectiveState: "temporarily_unavailable",
            errorCode: "keychain_unavailable"
        )
        let item = ProviderCenterItem(
            descriptor: try descriptor("step_plan"),
            instanceCount: 1,
            observed: true,
            issues: [keychain]
        )

        #expect(keychain.requiresUserAction)
        #expect(keychain.message == "Current quota needs a valid connection.")
        #expect(item.status == .attention)
        #expect(item.connectionIssues == [keychain])
        #expect(item.secondaryIssues.isEmpty)
    }

    @Test("Configured connection identity wins over a colliding discovered family")
    func configuredConnectionOwnsSourceHealth() {
        #expect(ProviderCenterPresentation.sourceFamilyID(
            providerID: "step-plan-main",
            configuredFamilies: ["step-plan-main": "step_plan"],
            discoveredFamilyID: "step_plan_main"
        ) == "step_plan")
        #expect(ProviderCenterPresentation.sourceFamilyID(
            providerID: "cursor",
            configuredFamilies: ["step-plan-main": "step_plan"],
            discoveredFamilyID: "cursor"
        ) == "cursor")
    }

    @Test("Secondary source failures keep a connected Provider healthy")
    func secondaryIssuesDoNotEscalateProvider() throws {
        let tokenHistory = issue(
            sourceID: "openusage.daily", effectiveState: "stale", errorCode: "timeout"
        )
        let quota = issue(sourceID: "current.quota", effectiveState: "ok", errorCode: nil)
        let item = ProviderCenterItem(
            descriptor: try descriptor("minimax"), instanceCount: 1,
            observed: true, issues: [tokenHistory, quota]
        )

        #expect(item.status == .connected)
        #expect(item.secondaryIssues.map(\.message) == ["Daily token history is stale."])
        #expect(item.helpText == "Daily token history is stale.")
    }

    @Test("Credential failures are the only source failures promoted to the Provider list")
    func credentialIssuesRequireAttention() throws {
        let item = ProviderCenterItem(
            descriptor: try descriptor("step_plan"), instanceCount: 1,
            observed: true,
            issues: [issue(
                sourceID: "current.quota", effectiveState: "auth_expired",
                errorCode: "quota_unavailable"
            )]
        )

        #expect(item.status == .attention)
        #expect(item.connectionIssues.count == 1)
        #expect(item.helpText == "Current quota needs a valid connection.")
    }

    @Test("OpenUsage is presented as a system integration instead of a Provider")
    func systemIntegrationClassification() {
        #expect(ProviderCenterPresentation.isSystemIntegration("openusage"))
        #expect(ProviderCenterPresentation.isSystemIntegration("openusage_catalog"))
        #expect(!ProviderCenterPresentation.isSystemIntegration("minimax"))
    }

    private func issue(
        sourceID: String = "current.quota",
        effectiveState: String = "temporarily_unavailable",
        errorCode: String? = "quota_unavailable"
    ) -> ProviderSourceIssuePresentation {
        ProviderSourceIssuePresentation.make(from: SourceHealthItem(
            providerID: "example", sourceID: sourceID,
            state: effectiveState, effectiveState: effectiveState,
            lastAttemptAt: "2026-07-17T10:00:00Z",
            lastSuccessAt: "2026-07-17T09:00:00Z",
            staleAt: nil, errorCode: errorCode
        ))
    }

    private func descriptor(_ familyID: String) throws -> ProviderDisplayDescriptor {
        try #require(ProviderCatalog.allDescriptors.first { $0.familyID == familyID })
    }

    private func mutationObject(_ request: ProviderMutationRequestV2) throws -> [String: Any] {
        try #require(
            JSONSerialization.jsonObject(with: JSONEncoder().encode(request))
                as? [String: Any]
        )
    }
}
