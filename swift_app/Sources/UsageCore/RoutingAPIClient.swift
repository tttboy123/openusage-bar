import Foundation

public enum RoutingAPIClientError: Error, Sendable, Equatable {
    case unavailable
    case timedOut
    case responseTooLarge
    case invalidRequest
    case invalidResponse
    case schemaMismatch
    case serverUnavailable
}

public enum RoutingTaskKind: String, Codable, CaseIterable, Sendable {
    case audio, chat, code, embedding, image, other, reasoning
}

public enum RoutingPrivacy: String, Codable, CaseIterable, Sendable {
    case localOnly = "local_only"
    case directProvider = "direct_provider"
    case allowProxy = "allow_proxy"
}

public struct RoutingCapability: RawRepresentable, Codable, Hashable, Sendable {
    public let rawValue: String

    public init(rawValue: String) { self.rawValue = rawValue }

    public static let chat = Self(rawValue: "chat")
    public static let reasoning = Self(rawValue: "reasoning")
    public static let tools = Self(rawValue: "tools")
    public static let embedding = Self(rawValue: "embedding")
    public static let image = Self(rawValue: "image")
    public static let audio = Self(rawValue: "audio")
}

public struct RoutingTask: Codable, Hashable, Sendable {
    public let kind: RoutingTaskKind
    public let requiredCapabilities: [RoutingCapability]
    public let estimatedInputTokens: Int64
    public let maxOutputTokens: Int64
    public let minimumContextWindowTokens: Int64
    public let privacy: RoutingPrivacy
    public let regions: [String]

    public init(
        kind: RoutingTaskKind,
        requiredCapabilities: [RoutingCapability] = [.chat],
        estimatedInputTokens: Int64 = 0,
        maxOutputTokens: Int64 = 0,
        minimumContextWindowTokens: Int64 = 0,
        privacy: RoutingPrivacy = .directProvider,
        regions: [String] = ["global"]
    ) {
        self.kind = kind
        self.requiredCapabilities = requiredCapabilities
        self.estimatedInputTokens = estimatedInputTokens
        self.maxOutputTokens = maxOutputTokens
        self.minimumContextWindowTokens = minimumContextWindowTokens
        self.privacy = privacy
        self.regions = regions
    }
}

public struct RoutingConstraints: Codable, Hashable, Sendable {
    public let allowProviders: [String]
    public let denyProviders: [String]
    public let allowTargets: [String]
    public let denyTargets: [String]
    public let maximumEstimatedCostMicrounits: Int64?
    public let costCurrency: String?

    public init(
        allowProviders: [String] = [], denyProviders: [String] = [],
        allowTargets: [String] = [], denyTargets: [String] = [],
        maximumEstimatedCostMicrounits: Int64? = nil,
        costCurrency: String? = nil
    ) {
        self.allowProviders = allowProviders
        self.denyProviders = denyProviders
        self.allowTargets = allowTargets
        self.denyTargets = denyTargets
        self.maximumEstimatedCostMicrounits = maximumEstimatedCostMicrounits
        self.costCurrency = costCurrency
    }
}

public struct RoutingSession: Codable, Hashable, Sendable {
    public let sessionRef: String
    public let remainingBudgetMicrounits: Int64?
    public let reserveMicrounits: Int64?
    public let budgetCurrency: String?

    public init(
        sessionRef: String,
        remainingBudgetMicrounits: Int64? = nil,
        reserveMicrounits: Int64? = nil,
        budgetCurrency: String? = nil
    ) {
        self.sessionRef = sessionRef
        self.remainingBudgetMicrounits = remainingBudgetMicrounits
        self.reserveMicrounits = reserveMicrounits
        self.budgetCurrency = budgetCurrency
    }
}

public struct RoutingDecisionRequest: Encodable, Hashable, Sendable {
    public let schemaVersion = "1.0"
    public let clientRequestRef: String?
    public let policyID: String
    public let task: RoutingTask
    public let constraints: RoutingConstraints
    public let session: RoutingSession?

    public init(
        clientRequestRef: String? = nil,
        policyID: String,
        task: RoutingTask,
        constraints: RoutingConstraints = RoutingConstraints(),
        session: RoutingSession? = nil
    ) {
        self.clientRequestRef = clientRequestRef
        self.policyID = policyID
        self.task = task
        self.constraints = constraints
        self.session = session
    }

    public static func minimal(policyID: String = "reliable") -> Self {
        Self(policyID: policyID, task: RoutingTask(kind: .code))
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion, clientRequestRef, task, constraints, session
        case policyID = "policyId"
    }
}

public struct RoutingHealth: Sendable, Hashable {
    public let ok: Bool
    public let status: String
    public let targetRevision: Int64
    public let targetCount: Int
    public let routingRevision: Int64
}

public struct RoutingPolicyWeights: Codable, Sendable, Hashable {
    public let reliability: Int
    public let headroom: Int
    public let latency: Int
    public let cost: Int
}

public struct RoutingPolicyRequirements: Codable, Sendable, Hashable {
    public let minimumHeadroomBasisPoints: Int
    public let maximumErrorRateBasisPoints: Int
    public let minimumRuntimeSamples: Int
    public let requireCost: Bool
    public let requireRuntime: Bool
}

public struct RoutingPolicy: Decodable, Sendable, Hashable, Identifiable {
    public let policyID: String
    public let revision: Int64
    public let weights: RoutingPolicyWeights
    public let requirements: RoutingPolicyRequirements
    public var id: String { policyID }

    enum CodingKeys: String, CodingKey {
        case weights, requirements
        case policyID = "policyId"
        case revision = "policyRevision"
    }
}

public struct RoutingTarget: Decodable, Sendable, Hashable, Identifiable {
    public let targetID: String
    public let providerID: String
    public let accountRef: String
    public let modelID: String
    public let connectionRef: String
    public let executionClass: String
    public let executionAdapterID: String
    public let resourceMode: String
    public let factAccountRef: String?
    public let runtimeScopeRef: String?
    public let balanceCurrency: String?
    public let costCurrency: String?
    public let inputCostMicrosPerMillion: Int64?
    public let outputCostMicrosPerMillion: Int64?
    public let enabled: Bool
    public let adapterAvailable: Bool
    public let regions: [String]
    public let privacyClass: String
    public let capabilities: [String]
    public let contextWindowTokens: Int64
    public let qualityTier: Int
    public var id: String { targetID }

    enum CodingKeys: String, CodingKey {
        case accountRef, connectionRef, executionClass, resourceMode
        case factAccountRef, runtimeScopeRef, balanceCurrency, costCurrency
        case inputCostMicrosPerMillion, outputCostMicrosPerMillion, enabled
        case adapterAvailable, regions, privacyClass, capabilities
        case contextWindowTokens, qualityTier
        case targetID = "targetId"
        case providerID = "providerId"
        case modelID = "modelId"
        case executionAdapterID = "executionAdapterId"
    }
}

public struct RoutingTargetDocument: Sendable, Hashable {
    public let revision: Int64
    public let targets: [RoutingTarget]
}

public struct RoutingScoreComponents: Codable, Sendable, Hashable {
    public let reliability: Int
    public let headroom: Int
    public let latency: Int
    public let cost: Int
}

public struct RoutingScoredTarget: Decodable, Sendable, Hashable, Identifiable {
    public let targetID: String
    public let providerID: String?
    public let accountRef: String?
    public let modelID: String?
    public let score: Int
    public let components: RoutingScoreComponents
    public let reasons: [String]
    public var id: String { targetID }

    enum CodingKeys: String, CodingKey {
        case score, components, reasons, accountRef
        case targetID = "targetId"
        case providerID = "providerId"
        case modelID = "modelId"
    }
}

public struct RoutingRejectedTarget: Decodable, Sendable, Hashable, Identifiable {
    public let targetID: String
    public let reasonCodes: [String]
    public var id: String { targetID }

    enum CodingKeys: String, CodingKey {
        case reasonCodes
        case targetID = "targetId"
    }
}

public struct RoutingPolicyReference: Codable, Sendable, Hashable {
    public let policyID: String
    public let revision: Int64

    enum CodingKeys: String, CodingKey {
        case policyID = "policyId"
        case revision = "policyRevision"
    }
}

public struct RoutingFactReference: Codable, Sendable, Hashable {
    public let dataRevision: UInt64
    public let runtimeRevision: UInt64?
}

public struct RoutingDecision: Sendable, Hashable, Identifiable {
    public let decisionID: String
    public let generatedAt: String
    public let expiresAt: String
    public let policy: RoutingPolicyReference
    public let facts: RoutingFactReference
    public let selected: RoutingScoredTarget?
    public let alternatives: [RoutingScoredTarget]
    public let rejected: [RoutingRejectedTarget]
    public let warnings: [String]
    public let evidenceStored: Bool
    public let simulated: Bool
    public var id: String { decisionID }
}

public struct RoutingHistoryDecision: Decodable, Sendable, Hashable, Identifiable {
    public let decisionID: String
    public let generatedAt: String
    public let expiresAt: String
    public let policy: RoutingPolicyReference
    public let facts: RoutingFactReference
    public let clientRequestRef: String?
    public let sessionRef: String?
    public let selected: RoutingScoredTarget?
    public let alternatives: [RoutingScoredTarget]
    public let rejected: [RoutingRejectedTarget]
    public var id: String { decisionID }

    enum CodingKeys: String, CodingKey {
        case generatedAt, expiresAt, policy, facts, clientRequestRef, sessionRef
        case selected, alternatives, rejected
        case decisionID = "decisionId"
    }
}

public struct RoutingHistoryPage: Sendable, Hashable {
    public let revision: Int64
    public let decisions: [RoutingHistoryDecision]
    public let nextBefore: String?
}

public protocol RoutingAPIReading: Sendable {
    func health() async throws -> RoutingHealth
    func policies() async throws -> [RoutingPolicy]
    func targets() async throws -> RoutingTargetDocument
    func simulate(_ request: RoutingDecisionRequest) async throws -> RoutingDecision
    func history(before: String?, limit: Int) async throws -> RoutingHistoryPage
}

public extension RoutingAPIReading {
    func history(limit: Int) async throws -> RoutingHistoryPage {
        try await history(before: nil, limit: limit)
    }
}

public struct RoutingAPIClient: RoutingAPIReading, Sendable {
    public static let defaultSocketURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".local/state/openusage-bar/router.sock")

    private let socketURL: URL
    private let timeoutSeconds: Double
    private let maximumBodyBytes: Int

    public init(
        socketURL: URL = Self.defaultSocketURL,
        timeout: Duration = .seconds(3),
        maximumBodyBytes: Int = 262_144
    ) {
        self.socketURL = socketURL
        let components = timeout.components
        timeoutSeconds = max(
            0,
            Double(components.seconds)
                + Double(components.attoseconds) / 1_000_000_000_000_000_000
        )
        self.maximumBodyBytes = maximumBodyBytes
    }

    public func health() async throws -> RoutingHealth {
        let wire = try await get(HealthWire.self, target: "/v1/health")
        try requireSchema(wire.schemaVersion)
        guard wire.targetRevision >= 0, wire.targetCount >= 0, wire.targetCount <= 128,
              wire.routingRevision >= 0, Self.safeID(wire.health.status)
        else { throw RoutingAPIClientError.invalidResponse }
        return RoutingHealth(
            ok: wire.health.ok, status: wire.health.status,
            targetRevision: wire.targetRevision, targetCount: wire.targetCount,
            routingRevision: wire.routingRevision
        )
    }

    public func policies() async throws -> [RoutingPolicy] {
        let wire = try await get(PoliciesWire.self, target: "/v1/policies")
        try requireSchema(wire.schemaVersion)
        guard wire.policies.count <= 128,
              Set(wire.policies.map(\.policyID)).count == wire.policies.count,
              wire.policies.allSatisfy(Self.validPolicy)
        else { throw RoutingAPIClientError.invalidResponse }
        return wire.policies
    }

    public func targets() async throws -> RoutingTargetDocument {
        let wire = try await get(TargetsWire.self, target: "/v1/targets")
        try requireSchema(wire.schemaVersion)
        guard wire.targetRevision >= 0, wire.targets.count <= 128,
              Set(wire.targets.map(\.targetID)).count == wire.targets.count,
              wire.targets.allSatisfy(Self.validTarget)
        else { throw RoutingAPIClientError.invalidResponse }
        return RoutingTargetDocument(revision: wire.targetRevision, targets: wire.targets)
    }

    public func decide(_ request: RoutingDecisionRequest) async throws -> RoutingDecision {
        try await postDecision(request, target: "/v1/decisions")
    }

    public func simulate(_ request: RoutingDecisionRequest) async throws -> RoutingDecision {
        try await postDecision(request, target: "/v1/simulations")
    }

    public func history(before: String? = nil, limit: Int) async throws -> RoutingHistoryPage {
        guard (1...100).contains(limit), before.map(Self.safeID) ?? true else {
            throw RoutingAPIClientError.invalidRequest
        }
        let target = "/v1/decisions?limit=\(limit)" + (before.map { "&before=\($0)" } ?? "")
        let wire = try await get(HistoryWire.self, target: target)
        try requireSchema(wire.schemaVersion)
        guard wire.routingRevision >= 0, wire.decisions.count <= limit,
              wire.nextBefore.map(Self.safeID) ?? true,
              wire.decisions.allSatisfy(Self.validHistoryDecision)
        else { throw RoutingAPIClientError.invalidResponse }
        return RoutingHistoryPage(
            revision: wire.routingRevision,
            decisions: wire.decisions,
            nextBefore: wire.nextBefore
        )
    }

    private func postDecision(
        _ request: RoutingDecisionRequest, target: String
    ) async throws -> RoutingDecision {
        guard Self.valid(request) else { throw RoutingAPIClientError.invalidRequest }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        guard let body = try? encoder.encode(request), body.count <= 65_536 else {
            throw RoutingAPIClientError.invalidRequest
        }
        let response = try await transport(method: "POST", target: target, body: body)
        if response.statusCode == 200 {
            let wire: DecisionWire = try decode(DecisionWire.self, from: response.body)
            try requireSchema(wire.schemaVersion)
            let decision = wire.value
            guard Self.valid(decision) else { throw RoutingAPIClientError.invalidResponse }
            return decision
        }
        if response.statusCode == 409 {
            let wire: NoRouteWire = try decode(NoRouteWire.self, from: response.body)
            try requireSchema(wire.schemaVersion)
            guard wire.error.code == "no_route" else {
                throw RoutingAPIClientError.invalidResponse
            }
            let details = wire.error.details
            let decision = RoutingDecision(
                decisionID: details.decisionID,
                generatedAt: details.generatedAt,
                expiresAt: details.expiresAt,
                policy: details.policy,
                facts: details.facts,
                selected: nil,
                alternatives: [],
                rejected: details.rejected,
                warnings: [],
                evidenceStored: details.evidenceStored,
                simulated: details.simulated
            )
            guard Self.valid(decision) else { throw RoutingAPIClientError.invalidResponse }
            return decision
        }
        if response.statusCode == 503 { throw RoutingAPIClientError.serverUnavailable }
        throw RoutingAPIClientError.invalidResponse
    }

    private func get<T: Decodable>(_ type: T.Type, target: String) async throws -> T {
        let response = try await transport(method: "GET", target: target)
        guard response.statusCode == 200 else {
            if response.statusCode == 503 { throw RoutingAPIClientError.serverUnavailable }
            throw RoutingAPIClientError.invalidResponse
        }
        return try decode(type, from: response.body)
    }

    private func transport(
        method: String, target: String, body: Data? = nil
    ) async throws -> UnixHTTPResponse {
        guard timeoutSeconds > 0, maximumBodyBytes > 0 else {
            throw RoutingAPIClientError.invalidRequest
        }
        do {
            return try await Task.detached(priority: .utility) {
                try UnixHTTPTransport(
                    socketURL: socketURL, timeoutSeconds: timeoutSeconds,
                    maximumBodyBytes: maximumBodyBytes
                ).request(method: method, target: target, body: body)
            }.value
        } catch let error as LocalAPIClientError {
            switch error {
            case .unavailable: throw RoutingAPIClientError.unavailable
            case .timedOut: throw RoutingAPIClientError.timedOut
            case .responseTooLarge: throw RoutingAPIClientError.responseTooLarge
            case .invalidResponse: throw RoutingAPIClientError.invalidResponse
            case .schemaMismatch: throw RoutingAPIClientError.schemaMismatch
            }
        } catch {
            throw RoutingAPIClientError.unavailable
        }
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        do { return try JSONDecoder().decode(type, from: data) }
        catch { throw RoutingAPIClientError.invalidResponse }
    }

    private func requireSchema(_ value: String) throws {
        guard value == "1.0" else { throw RoutingAPIClientError.schemaMismatch }
    }

    private static func valid(_ request: RoutingDecisionRequest) -> Bool {
        safeID(request.policyID)
            && request.clientRequestRef.map({ $0.range(of: #"^req_[0-9a-f]{16,64}$"#, options: .regularExpression) != nil }) ?? true
            && validIDs(request.task.requiredCapabilities.map(\.rawValue))
            && validCounter(request.task.estimatedInputTokens)
            && validCounter(request.task.maxOutputTokens)
            && validCounter(request.task.minimumContextWindowTokens)
            && validIDs(request.task.regions)
            && validIDs(request.constraints.allowProviders)
            && validIDs(request.constraints.denyProviders)
            && validIDs(request.constraints.allowTargets)
            && validIDs(request.constraints.denyTargets)
            && request.constraints.maximumEstimatedCostMicrounits.map(validCounter) ?? true
            && request.constraints.costCurrency.map(safeCurrency) ?? true
            && request.session.map(validSession) ?? true
    }

    private static func validSession(_ session: RoutingSession) -> Bool {
        session.sessionRef.range(of: #"^anon_[0-9a-f]{16,64}$"#, options: .regularExpression) != nil
            && session.remainingBudgetMicrounits.map(validCounter) ?? true
            && session.reserveMicrounits.map(validCounter) ?? true
            && session.budgetCurrency.map(safeCurrency) ?? true
    }

    private static func validPolicy(_ value: RoutingPolicy) -> Bool {
        let weights = value.weights
        let values = [weights.reliability, weights.headroom, weights.latency, weights.cost]
        return safeID(value.policyID) && value.revision > 0
            && values.allSatisfy { (0...100).contains($0) }
            && values.reduce(0, +) == 100
            && (0...10_000).contains(value.requirements.minimumHeadroomBasisPoints)
            && (0...10_000).contains(value.requirements.maximumErrorRateBasisPoints)
            && value.requirements.minimumRuntimeSamples >= 0
    }

    private static func validTarget(_ value: RoutingTarget) -> Bool {
        let ids = [value.targetID, value.providerID, value.accountRef, value.modelID,
                   value.connectionRef, value.executionAdapterID]
        let allowedExecution = ["direct_api", "subscription_cli", "openai_compatible", "self_hosted"]
        let allowedModes = ["quota", "balance"]
        let allowedPrivacy = ["local_only", "direct_provider", "proxy"]
        let costs = [value.inputCostMicrosPerMillion, value.outputCostMicrosPerMillion]
        return ids.allSatisfy(safeID)
            && allowedExecution.contains(value.executionClass)
            && allowedModes.contains(value.resourceMode)
            && allowedPrivacy.contains(value.privacyClass)
            && value.factAccountRef.map(safeID) ?? true
            && value.runtimeScopeRef.map({ $0.range(of: #"^anon_[0-9a-f]{16,64}$"#, options: .regularExpression) != nil }) ?? true
            && value.balanceCurrency.map(safeCurrency) ?? true
            && value.costCurrency.map(safeCurrency) ?? true
            && costs.allSatisfy { $0.map(validCounter) ?? true }
            && (value.costCurrency == nil) == costs.allSatisfy { $0 == nil }
            && (value.resourceMode == "balance") == (value.balanceCurrency != nil)
            && validIDs(value.regions)
            && validIDs(value.capabilities)
            && validCounter(value.contextWindowTokens)
            && (0...5).contains(value.qualityTier)
    }

    private static func valid(_ value: RoutingDecision) -> Bool {
        safeID(value.decisionID) && safeTimestamp(value.generatedAt) && safeTimestamp(value.expiresAt)
            && safeID(value.policy.policyID) && value.policy.revision > 0
            && value.alternatives.count <= 16 && value.rejected.count <= 128
            && value.warnings.count <= 16
            && value.selected.map(validScored) ?? true
            && value.alternatives.allSatisfy(validScored)
            && value.rejected.allSatisfy(validRejected)
    }

    private static func validHistoryDecision(_ value: RoutingHistoryDecision) -> Bool {
        safeID(value.decisionID) && safeTimestamp(value.generatedAt) && safeTimestamp(value.expiresAt)
            && safeID(value.policy.policyID) && value.policy.revision > 0
            && value.clientRequestRef.map({ $0.hasPrefix("req_") && safeID($0) }) ?? true
            && value.sessionRef.map({ $0.hasPrefix("anon_") && safeID($0) }) ?? true
            && value.alternatives.count <= 16 && value.rejected.count <= 128
            && value.selected.map(validScored) ?? true
            && value.alternatives.allSatisfy(validScored)
            && value.rejected.allSatisfy(validRejected)
    }

    private static func validScored(_ value: RoutingScoredTarget) -> Bool {
        let components = value.components
        return safeID(value.targetID) && value.providerID.map(safeID) ?? true
            && value.accountRef.map(safeID) ?? true && value.modelID.map(safeID) ?? true
            && (0...10_000).contains(value.score)
            && [components.reliability, components.headroom, components.latency, components.cost]
                .allSatisfy { (0...10_000).contains($0) }
            && value.reasons.count <= 16 && validIDs(value.reasons)
    }

    private static func validRejected(_ value: RoutingRejectedTarget) -> Bool {
        safeID(value.targetID) && !value.reasonCodes.isEmpty
            && value.reasonCodes.count <= 16 && validIDs(value.reasonCodes)
    }

    private static func validCounter(_ value: Int64) -> Bool {
        (0...1_000_000_000_000).contains(value)
    }

    private static func validIDs(_ values: [String]) -> Bool {
        values.count <= 50 && Set(values).count == values.count && values.allSatisfy(safeID)
    }

    private static func safeID(_ value: String) -> Bool {
        value.range(of: #"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"#, options: .regularExpression) != nil
    }

    private static func safeCurrency(_ value: String) -> Bool {
        value.range(of: #"^[A-Z][A-Z0-9_]{2,7}$"#, options: .regularExpression) != nil
    }

    private static func safeTimestamp(_ value: String) -> Bool {
        value.utf8.count <= 64 && value.hasSuffix("Z")
            && ISO8601DateFormatter().date(from: value) != nil
    }
}

private struct HealthWire: Decodable {
    struct Health: Decodable { let ok: Bool; let status: String }
    let schemaVersion: String
    let health: Health
    let targetRevision: Int64
    let targetCount: Int
    let routingRevision: Int64
}

private struct PoliciesWire: Decodable {
    let schemaVersion: String
    let policies: [RoutingPolicy]
}

private struct TargetsWire: Decodable {
    let schemaVersion: String
    let targetRevision: Int64
    let targets: [RoutingTarget]
}

private struct DecisionWire: Decodable {
    let schemaVersion: String
    let decisionID: String
    let generatedAt: String
    let expiresAt: String
    let policy: RoutingPolicyReference
    let facts: RoutingFactReference
    let selected: RoutingScoredTarget?
    let alternatives: [RoutingScoredTarget]
    let rejected: [RoutingRejectedTarget]
    let warnings: [String]
    let evidenceStored: Bool
    let simulated: Bool

    var value: RoutingDecision {
        RoutingDecision(
            decisionID: decisionID, generatedAt: generatedAt, expiresAt: expiresAt,
            policy: policy, facts: facts, selected: selected,
            alternatives: alternatives, rejected: rejected, warnings: warnings,
            evidenceStored: evidenceStored, simulated: simulated
        )
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion, generatedAt, expiresAt, policy, facts, selected
        case alternatives, rejected, warnings, evidenceStored, simulated
        case decisionID = "decisionId"
    }
}

private struct NoRouteWire: Decodable {
    struct Problem: Decodable {
        let code: String
        let message: String
        let details: Details
    }
    struct Details: Decodable {
        let decisionID: String
        let generatedAt: String
        let expiresAt: String
        let policy: RoutingPolicyReference
        let facts: RoutingFactReference
        let rejected: [RoutingRejectedTarget]
        let evidenceStored: Bool
        let simulated: Bool

        enum CodingKeys: String, CodingKey {
            case generatedAt, expiresAt, policy, facts, rejected, evidenceStored, simulated
            case decisionID = "decisionId"
        }
    }
    let schemaVersion: String
    let error: Problem
}

private struct HistoryWire: Decodable {
    let schemaVersion: String
    let routingRevision: Int64
    let decisions: [RoutingHistoryDecision]
    let nextBefore: String?
}
