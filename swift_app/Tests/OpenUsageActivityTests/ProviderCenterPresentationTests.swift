import Testing
import UsageCore
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
        #expect(ProviderCenterText.scope(try descriptor("minimax")) == "中国站和国际站")
        #expect(ProviderCenterText.scope(try descriptor("step_plan")) == "中国站和国际站")
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
                observed: true, needsAttention: false
            ),
            ProviderCenterItem(
                descriptor: try descriptor("alibaba_cloud"), instanceCount: 0,
                observed: false, needsAttention: false
            ),
            ProviderCenterItem(
                descriptor: try descriptor("openclaw"), instanceCount: 0,
                observed: true, needsAttention: true
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
            observed: false, needsAttention: false
        ).status == .available)
        #expect(ProviderCenterItem(
            descriptor: try descriptor("minimax"), instanceCount: 1,
            observed: true, needsAttention: false
        ).status == .connected)
        #expect(ProviderCenterItem(
            descriptor: try descriptor("openclaw"), instanceCount: 0,
            observed: true, needsAttention: true
        ).status == .attention)
    }

    private func descriptor(_ familyID: String) throws -> ProviderDisplayDescriptor {
        try #require(ProviderCatalog.allDescriptors.first { $0.familyID == familyID })
    }
}
