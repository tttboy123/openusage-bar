import Foundation
import UsageCore

struct RoutingTargetMutationValue: Encodable, Sendable, Hashable {
    let targetID: String
    let providerID: String
    let accountRef: String
    let modelID: String
    let connectionRef: String
    let executionClass: String
    let executionAdapterID: String
    let resourceMode: String
    let factAccountRef: String?
    let runtimeScopeRef: String?
    let balanceCurrency: String?
    let costCurrency: String?
    let inputCostMicrosPerMillion: Int64?
    let outputCostMicrosPerMillion: Int64?
    let enabled: Bool
    let regions: [String]
    let privacyClass: String
    let capabilities: [String]
    let contextWindowTokens: Int64
    let qualityTier: Int

    init(
        targetID: String,
        providerID: String,
        accountRef: String,
        modelID: String,
        connectionRef: String,
        executionClass: String,
        executionAdapterID: String,
        resourceMode: String,
        factAccountRef: String?,
        runtimeScopeRef: String?,
        balanceCurrency: String?,
        costCurrency: String?,
        inputCostMicrosPerMillion: Int64?,
        outputCostMicrosPerMillion: Int64?,
        enabled: Bool,
        regions: [String],
        privacyClass: String,
        capabilities: [String],
        contextWindowTokens: Int64,
        qualityTier: Int
    ) {
        self.targetID = targetID
        self.providerID = providerID
        self.accountRef = accountRef
        self.modelID = modelID
        self.connectionRef = connectionRef
        self.executionClass = executionClass
        self.executionAdapterID = executionAdapterID
        self.resourceMode = resourceMode
        self.factAccountRef = factAccountRef
        self.runtimeScopeRef = runtimeScopeRef
        self.balanceCurrency = balanceCurrency
        self.costCurrency = costCurrency
        self.inputCostMicrosPerMillion = inputCostMicrosPerMillion
        self.outputCostMicrosPerMillion = outputCostMicrosPerMillion
        self.enabled = enabled
        self.regions = regions
        self.privacyClass = privacyClass
        self.capabilities = capabilities
        self.contextWindowTokens = contextWindowTokens
        self.qualityTier = qualityTier
    }

    init(target: RoutingTarget, enabled: Bool? = nil) {
        targetID = target.targetID
        providerID = target.providerID
        accountRef = target.accountRef
        modelID = target.modelID
        connectionRef = target.connectionRef
        executionClass = target.executionClass
        executionAdapterID = target.executionAdapterID
        resourceMode = target.resourceMode
        factAccountRef = target.factAccountRef
        runtimeScopeRef = target.runtimeScopeRef
        balanceCurrency = target.balanceCurrency
        costCurrency = target.costCurrency
        inputCostMicrosPerMillion = target.inputCostMicrosPerMillion
        outputCostMicrosPerMillion = target.outputCostMicrosPerMillion
        self.enabled = enabled ?? target.enabled
        regions = target.regions
        privacyClass = target.privacyClass
        capabilities = target.capabilities
        contextWindowTokens = target.contextWindowTokens
        qualityTier = target.qualityTier
    }

    enum CodingKeys: String, CodingKey {
        case accountRef, connectionRef, executionClass, resourceMode
        case factAccountRef, runtimeScopeRef, balanceCurrency, costCurrency
        case inputCostMicrosPerMillion, outputCostMicrosPerMillion, enabled
        case regions, privacyClass, capabilities, contextWindowTokens, qualityTier
        case targetID = "targetId"
        case providerID = "providerId"
        case modelID = "modelId"
        case executionAdapterID = "executionAdapterId"
    }
}

struct RoutingTargetMutationRequest: Encodable, Sendable, Hashable {
    let version = 1
    let action = "replace_targets"
    let expectedRevision: Int64
    let targets: [RoutingTargetMutationValue]

    enum CodingKeys: String, CodingKey {
        case version, action, expectedRevision, targets
    }
}

struct RoutingMutationResponse: Decodable, Sendable, Hashable {
    let version: Int
    let ok: Bool
    let message: String
    let targetRevision: Int64?
    let connectionRevision: Int64?
    let connections: [RoutingExecutionConnection]?
    let policyDocumentRevision: Int64?
    let customPolicies: [RoutingCustomPolicy]?
    let preferencesRevision: Int64?
    let routingPreferences: RoutingPreferencesValue?
    let providerExecutionTemplates: [RoutingProviderExecutionTemplate]?

    init(
        version: Int,
        ok: Bool,
        message: String,
        targetRevision: Int64? = nil,
        connectionRevision: Int64? = nil,
        connections: [RoutingExecutionConnection]? = nil,
        policyDocumentRevision: Int64? = nil,
        customPolicies: [RoutingCustomPolicy]? = nil,
        preferencesRevision: Int64? = nil,
        routingPreferences: RoutingPreferencesValue? = nil,
        providerExecutionTemplates: [RoutingProviderExecutionTemplate]? = nil
    ) {
        self.version = version
        self.ok = ok
        self.message = message
        self.targetRevision = targetRevision
        self.connectionRevision = connectionRevision
        self.connections = connections
        self.policyDocumentRevision = policyDocumentRevision
        self.customPolicies = customPolicies
        self.preferencesRevision = preferencesRevision
        self.routingPreferences = routingPreferences
        self.providerExecutionTemplates = providerExecutionTemplates
    }
}

protocol RoutingMutationSubmitting: Sendable {
    func submit(
        _ request: RoutingTargetMutationRequest,
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure>
}

struct RoutingMutationClient: RoutingMutationSubmitting, Sendable {
    let limits: ProviderMutationLimits
    private let environment: [String: String]

    init(
        limits: ProviderMutationLimits = .production,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        self.limits = limits
        self.environment = ChildProcessEnvironment.sanitized(environment)
    }

    func submit(
        _ request: RoutingTargetMutationRequest,
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure> {
        await Task.detached(priority: .userInitiated) {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            guard limits.maximumResponseBytes > 0,
                  let requestData = try? encoder.encode(request),
                  requestData.count <= 256 * 1024
            else { return .failure(.invalidResponse) }

            switch ProviderMutationProcessRunner(environment: environment).run(
                requestData, command: command, limits: limits
            ) {
            case let .failure(failure):
                return .failure(failure)
            case let .success(responseData):
                guard String(data: responseData, encoding: .utf8) != nil,
                      let response = try? JSONDecoder().decode(
                          RoutingMutationResponse.self, from: responseData
                      ),
                      response.version == 1,
                      !response.message.isEmpty,
                      response.message.utf8.count <= 256,
                      response.message.unicodeScalars.allSatisfy({
                          $0.value >= 0x20 && $0.value != 0x7f
                      }),
                      response.ok == (response.targetRevision != nil),
                      response.targetRevision.map({ $0 > 0 }) ?? true,
                      response.connectionRevision == nil,
                      response.connections == nil,
                      response.policyDocumentRevision == nil,
                      response.customPolicies == nil,
                      response.preferencesRevision == nil,
                      response.routingPreferences == nil,
                      response.providerExecutionTemplates == nil
                else { return .failure(.invalidResponse) }
                return .success(response)
            }
        }.value
    }
}
