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
            replacementCredential: "private-key"
        )
        let create = try mutationObject(draft.request(action: .createConnection))
        #expect(create["version"] as? Int == 2)
        #expect(create["action"] as? String == "create_connection")
        #expect(create["kind"] as? String == "minimax")
        #expect((create["configuration"] as? [String: Any])?["name"] as? String == "MiniMax Work")
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
            replacementCredential: ""
        )
        #expect(missingCredential.validation(action: .createConnection) == .missingCredential)
        #expect(missingCredential.validation(action: .updateConnection) == nil)
    }

    @Test("Auto-discovered connections never receive mutation actions")
    func readOnlyDiscovery() {
        #expect(!ProviderCenterPresentation.canMutate(kind: "codex"))
        #expect(!ProviderCenterPresentation.canMutate(kind: "cursor"))
        #expect(ProviderCenterPresentation.canMutate(kind: "minimax"))
        #expect(ProviderCenterPresentation.canMutate(kind: "daily_usage_feed"))
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
