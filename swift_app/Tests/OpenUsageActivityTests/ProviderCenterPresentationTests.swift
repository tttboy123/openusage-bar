import Testing
@testable import UsageCore
@testable import OpenUsageActivity

@Suite("Provider Center presentation")
struct ProviderCenterPresentationTests {
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
}
