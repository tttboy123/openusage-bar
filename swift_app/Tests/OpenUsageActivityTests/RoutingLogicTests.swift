import Foundation
import Testing
@testable import UsageCore
@testable import OpenUsageActivity

@Suite("Native routing presentation")
@MainActor
struct RoutingLogicTests {
    @Test("Routing has a stable navigation route and does not load the usage ledger")
    func navigation() {
        #expect(UsageDetailsRoute(routeValue: "routing") == .routing)
        #expect(UsageDetailsRoute.routing.transportValue == "routing")
        #expect(UsageDetailsRoute.routing.title == AppLocalization.text("Routing"))
        #expect(!ActivityRouteLoadingPolicy.loadsLedgerOnAppear(.routing))
        #expect(!ActivityRouteLoadingPolicy.loadsLedgerAfterSelection(
            from: .automation, to: .routing
        ))
    }

    @Test("Load presents policies targets and content-free history")
    func load() async {
        let client = RoutingClientFixture()
        let model = RoutingViewModel(client: client)
        await model.load()
        #expect(model.health?.ok == true)
        #expect(model.policies.map(\.policyID) == ["reliable"])
        #expect(model.targets.map(\.targetID) == ["openai.work.gpt-5"])
        #expect(model.history.first?.selected?.targetID == "openai.work.gpt-5")
        #expect(model.selectedPolicyID == "reliable")
        #expect(model.failure == nil)
    }

    @Test("Dry run uses bounded metadata fields and keeps no-route explanations")
    func dryRun() async {
        let client = RoutingClientFixture(noRoute: true)
        let model = RoutingViewModel(client: client)
        model.estimatedInputTokens = 12_000
        model.maxOutputTokens = 4_000
        model.requiresReasoning = true
        model.requiresTools = true
        await model.simulate()
        #expect(model.decision?.selected == nil)
        #expect(model.decision?.rejected.first?.reasonCodes == ["fact_stale"])
        #expect(client.lastRequest?.task.estimatedInputTokens == 12_000)
        #expect(client.lastRequest?.task.requiredCapabilities == [.chat, .reasoning, .tools])
        #expect(model.failure == nil)
    }

    @Test("Errors are sanitized into stable human states")
    func sanitizedFailures() async {
        let client = RoutingClientFixture(error: .serverUnavailable)
        let model = RoutingViewModel(client: client)
        await model.load()
        #expect(model.failure == .serviceUnavailable)
        #expect(model.health == nil)
    }

    @Test("Target readiness is explicit and never upgrades a missing adapter")
    func readiness() {
        #expect(RoutingPresentation.readiness(target()).state == .ready)
        #expect(RoutingPresentation.readiness(target(adapterAvailable: false)).state == .adapterMissing)
        #expect(RoutingPresentation.reason("fact_stale") == AppLocalization.text("Usage facts are stale"))
    }

    @Test("Target enable changes use optimistic full-document replacement")
    func targetToggle() async {
        let client = RoutingClientFixture()
        let mutations = RoutingMutationFixture()
        let model = RoutingViewModel(client: client, mutations: mutations)
        await model.load()
        let command = ProviderMutationCommand(
            executableURL: URL(fileURLWithPath: "/tmp/helper"),
            arguments: ["routing-mutate"]
        )
        await model.setTargetEnabled("openai.work.gpt-5", enabled: false, command: command)
        #expect(mutations.lastRequest?.expectedRevision == 1)
        #expect(mutations.lastRequest?.targets.first?.enabled == false)
        #expect(model.mutationFailure == nil)
    }

    @Test("Execution connections load and save without exposing stored credentials")
    func connectionManagement() async {
        let connections = RoutingConnectionMutationFixture()
        let model = RoutingViewModel(
            client: RoutingClientFixture(),
            mutations: RoutingMutationFixture(),
            connectionMutations: connections
        )
        let command = ProviderMutationCommand(
            executableURL: URL(fileURLWithPath: "/tmp/helper"),
            arguments: ["routing-mutate"]
        )
        await model.loadConnections(command: command)
        #expect(model.connectionRevision == 2)
        #expect(model.connections.map(\.connectionRef) == ["conn_0123456789abcdef"])

        await model.upsertConnection(
            model.connections[0], secret: "replacement", command: command
        )
        #expect(connections.lastUpsert?.expectedRevision == 2)
        #expect(connections.lastUpsert?.secret == "replacement")
        #expect(model.mutationFailure == nil)
    }

    @Test("Connection drafts require an inference key only when first created")
    func connectionDraft() {
        var draft = RoutingConnectionDraft(
            generatedRef: "conn_0123456789abcdef"
        )
        draft.providerID = "glm"
        draft.accountRef = "account-1"
        draft.baseURL = "https://open.bigmodel.cn/api/paas/v4"
        draft.modelsText = "glm-4.5, glm-4.5-air"
        #expect(!draft.canSave)
        draft.secret = "replacement"
        #expect(draft.canSave)
        #expect(draft.connection.models == ["glm-4.5", "glm-4.5-air"])

        let existing = RoutingConnectionDraft(connection: .fixture())
        #expect(existing.secret.isEmpty)
        #expect(existing.canSave)
    }

    private func target(adapterAvailable: Bool = true) -> RoutingTarget {
        RoutingTarget.fixture(adapterAvailable: adapterAvailable)
    }
}

private final class RoutingConnectionMutationFixture:
    RoutingConnectionMutationSubmitting, @unchecked Sendable
{
    private let lock = NSLock()
    private var capturedUpsert: RoutingConnectionMutationRequest?
    var lastUpsert: RoutingConnectionMutationRequest? {
        lock.withLock { capturedUpsert }
    }

    func loadConnections(
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure> {
        let isFirstLoad = lock.withLock { capturedUpsert == nil }
        return .success(.init(
            version: 1, ok: true, message: "Execution connections loaded",
            connectionRevision: isFirstLoad ? 2 : 3,
            connections: [.fixture()]
        ))
    }

    func upsertConnection(
        _ request: RoutingConnectionMutationRequest,
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure> {
        lock.withLock { capturedUpsert = request }
        return .success(.init(
            version: 1, ok: true, message: "Execution connection saved",
            connectionRevision: request.expectedRevision + 1
        ))
    }

    func removeConnection(
        _ request: RoutingConnectionRemoveRequest,
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure> {
        .success(.init(
            version: 1, ok: true, message: "Execution connection removed",
            connectionRevision: request.expectedRevision + 1
        ))
    }
}

private final class RoutingMutationFixture: RoutingMutationSubmitting, @unchecked Sendable {
    private let lock = NSLock()
    private var capturedRequest: RoutingTargetMutationRequest?
    var lastRequest: RoutingTargetMutationRequest? { lock.withLock { capturedRequest } }

    func submit(
        _ request: RoutingTargetMutationRequest,
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure> {
        lock.withLock { capturedRequest = request }
        return .success(.init(
            version: 1, ok: true,
            message: "Routing targets saved", targetRevision: request.expectedRevision + 1
        ))
    }
}

private final class RoutingClientFixture: RoutingAPIReading, @unchecked Sendable {
    private let noRoute: Bool
    private let error: RoutingAPIClientError?
    private let lock = NSLock()
    private var capturedRequest: RoutingDecisionRequest?
    var lastRequest: RoutingDecisionRequest? { lock.withLock { capturedRequest } }

    init(noRoute: Bool = false, error: RoutingAPIClientError? = nil) {
        self.noRoute = noRoute
        self.error = error
    }

    func health() async throws -> RoutingHealth {
        if let error { throw error }
        return RoutingHealth.fixture()
    }

    func policies() async throws -> [RoutingPolicy] {
        if let error { throw error }
        return [.fixture()]
    }

    func targets() async throws -> RoutingTargetDocument {
        if let error { throw error }
        return RoutingTargetDocument(revision: 1, targets: [.fixture()])
    }

    func simulate(_ request: RoutingDecisionRequest) async throws -> RoutingDecision {
        if let error { throw error }
        lock.withLock { capturedRequest = request }
        return .fixture(noRoute: noRoute)
    }

    func history(before: String?, limit: Int) async throws -> RoutingHistoryPage {
        if let error { throw error }
        return RoutingHistoryPage(revision: 1, decisions: [.fixture()], nextBefore: nil)
    }
}

private extension RoutingHealth {
    static func fixture() -> Self {
        Self(ok: true, status: "ok", targetRevision: 1, targetCount: 1, routingRevision: 1)
    }
}

private extension RoutingExecutionConnection {
    static func fixture() -> Self {
        Self(
            connectionRef: "conn_0123456789abcdef",
            providerID: "openai", accountRef: "account-1",
            executionClass: "openai_compatible",
            executionAdapterID: "openai_compatible.direct",
            baseURL: "https://api.example.com/v1",
            enabled: true, models: ["gpt-5"]
        )
    }
}

private extension RoutingPolicyWeights {
    static func fixture() -> Self {
        Self(reliability: 50, headroom: 25, latency: 15, cost: 10)
    }
}

private extension RoutingPolicyRequirements {
    static func fixture() -> Self {
        Self(
            minimumHeadroomBasisPoints: 1_000,
            maximumErrorRateBasisPoints: 2_500,
            minimumRuntimeSamples: 0,
            requireCost: false,
            requireRuntime: false
        )
    }
}

private extension RoutingPolicy {
    static func fixture() -> Self {
        try! JSONDecoder().decode(Self.self, from: Data(#"{"policyId":"reliable","policyRevision":1,"weights":{"reliability":50,"headroom":25,"latency":15,"cost":10},"requirements":{"minimumHeadroomBasisPoints":1000,"maximumErrorRateBasisPoints":2500,"minimumRuntimeSamples":0,"requireCost":false,"requireRuntime":false}}"#.utf8))
    }
}

private extension RoutingTarget {
    static func fixture(adapterAvailable: Bool = true) -> Self {
        try! JSONDecoder().decode(Self.self, from: Data(#"{"targetId":"openai.work.gpt-5","providerId":"openai","accountRef":"account-1","modelId":"gpt-5","connectionRef":"connection-1","executionClass":"direct_api","executionAdapterId":"openai.direct","resourceMode":"quota","factAccountRef":"account-1","runtimeScopeRef":null,"balanceCurrency":null,"costCurrency":"USD","inputCostMicrosPerMillion":3000000,"outputCostMicrosPerMillion":3000000,"enabled":true,"adapterAvailable":\#(adapterAvailable),"regions":["global"],"privacyClass":"direct_provider","capabilities":["chat","reasoning","tools"],"contextWindowTokens":400000,"qualityTier":4}"#.utf8))
    }
}

private extension RoutingDecision {
    static func fixture(noRoute: Bool) -> Self {
        let rejected = noRoute
            ? [RoutingRejectedTarget.fixture()]
            : []
        return Self(
            decisionID: "route_0123456789abcdef0123456789abcdef",
            generatedAt: "2026-08-02T10:00:00Z",
            expiresAt: "2026-08-02T10:01:00Z",
            policy: .fixture(), facts: .fixture(),
            selected: noRoute ? nil : .fixture(), alternatives: [],
            rejected: rejected, warnings: [], evidenceStored: false, simulated: true
        )
    }
}

private extension RoutingPolicyReference {
    static func fixture() -> Self { Self(policyID: "reliable", revision: 1) }
}

private extension RoutingFactReference {
    static func fixture() -> Self { Self(dataRevision: 1, runtimeRevision: 2) }
}

private extension RoutingScoredTarget {
    static func fixture() -> Self {
        try! JSONDecoder().decode(Self.self, from: Data(#"{"targetId":"openai.work.gpt-5","score":8750,"components":{"reliability":5000,"headroom":2500,"latency":750,"cost":500},"reasons":["healthy_source"]}"#.utf8))
    }
}

private extension RoutingRejectedTarget {
    static func fixture() -> Self {
        try! JSONDecoder().decode(Self.self, from: Data(#"{"targetId":"minimax.primary.m3","reasonCodes":["fact_stale"]}"#.utf8))
    }
}

private extension RoutingHistoryDecision {
    static func fixture() -> Self {
        try! JSONDecoder().decode(Self.self, from: Data(#"{"decisionId":"route_0123456789abcdef0123456789abcdef","generatedAt":"2026-08-02T10:00:00Z","expiresAt":"2026-08-02T10:01:00Z","policy":{"policyId":"reliable","policyRevision":1},"facts":{"dataRevision":1,"runtimeRevision":2},"clientRequestRef":null,"sessionRef":null,"selected":{"targetId":"openai.work.gpt-5","score":8750,"components":{"reliability":5000,"headroom":2500,"latency":750,"cost":500},"reasons":["healthy_source"]},"alternatives":[],"rejected":[]}"#.utf8))
    }
}
