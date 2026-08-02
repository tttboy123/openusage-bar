import Foundation
import UsageCore

struct RoutingCustomPolicy: Codable, Sendable, Hashable, Identifiable {
    let policyID: String
    let policyRevision: Int64
    let reliabilityWeight: Int
    let headroomWeight: Int
    let latencyWeight: Int
    let costWeight: Int
    let minimumHeadroomBasisPoints: Int
    let maximumErrorRateBasisPoints: Int
    let minimumRuntimeSamples: Int64
    let latencyReferenceMilliseconds: Int64
    let costReferenceMicrounits: Int64
    let costCurrency: String
    let minimumBalanceMicrounits: Int64
    let balanceReferenceMicrounits: Int64
    let unknownPenaltyBasisPoints: Int
    let requireCost: Bool
    let requireRuntime: Bool
    let allowedExecutionClasses: [String]
    let allowedPrivacyClasses: [String]

    var id: String { policyID }

    enum CodingKeys: String, CodingKey {
        case policyRevision, reliabilityWeight, headroomWeight, latencyWeight
        case costWeight, minimumHeadroomBasisPoints, maximumErrorRateBasisPoints
        case minimumRuntimeSamples, latencyReferenceMilliseconds
        case costReferenceMicrounits, costCurrency, minimumBalanceMicrounits
        case balanceReferenceMicrounits, unknownPenaltyBasisPoints
        case requireCost, requireRuntime, allowedExecutionClasses
        case allowedPrivacyClasses
        case policyID = "policyId"
    }

    var isValid: Bool {
        RoutingPolicyMutationValue(policy: self).isValid && policyRevision > 0
            && policyRevision <= 1_000_000_000_000
    }
}

struct RoutingPolicyMutationValue: Codable, Sendable, Hashable {
    let policyID: String
    let reliabilityWeight: Int
    let headroomWeight: Int
    let latencyWeight: Int
    let costWeight: Int
    let minimumHeadroomBasisPoints: Int
    let maximumErrorRateBasisPoints: Int
    let minimumRuntimeSamples: Int64
    let latencyReferenceMilliseconds: Int64
    let costReferenceMicrounits: Int64
    let costCurrency: String
    let minimumBalanceMicrounits: Int64
    let balanceReferenceMicrounits: Int64
    let unknownPenaltyBasisPoints: Int
    let requireCost: Bool
    let requireRuntime: Bool
    let allowedExecutionClasses: [String]
    let allowedPrivacyClasses: [String]

    enum CodingKeys: String, CodingKey {
        case reliabilityWeight, headroomWeight, latencyWeight, costWeight
        case minimumHeadroomBasisPoints, maximumErrorRateBasisPoints
        case minimumRuntimeSamples, latencyReferenceMilliseconds
        case costReferenceMicrounits, costCurrency, minimumBalanceMicrounits
        case balanceReferenceMicrounits, unknownPenaltyBasisPoints
        case requireCost, requireRuntime, allowedExecutionClasses
        case allowedPrivacyClasses
        case policyID = "policyId"
    }

    init(policy: RoutingCustomPolicy) {
        policyID = policy.policyID
        reliabilityWeight = policy.reliabilityWeight
        headroomWeight = policy.headroomWeight
        latencyWeight = policy.latencyWeight
        costWeight = policy.costWeight
        minimumHeadroomBasisPoints = policy.minimumHeadroomBasisPoints
        maximumErrorRateBasisPoints = policy.maximumErrorRateBasisPoints
        minimumRuntimeSamples = policy.minimumRuntimeSamples
        latencyReferenceMilliseconds = policy.latencyReferenceMilliseconds
        costReferenceMicrounits = policy.costReferenceMicrounits
        costCurrency = policy.costCurrency
        minimumBalanceMicrounits = policy.minimumBalanceMicrounits
        balanceReferenceMicrounits = policy.balanceReferenceMicrounits
        unknownPenaltyBasisPoints = policy.unknownPenaltyBasisPoints
        requireCost = policy.requireCost
        requireRuntime = policy.requireRuntime
        allowedExecutionClasses = policy.allowedExecutionClasses
        allowedPrivacyClasses = policy.allowedPrivacyClasses
    }

    init(draft: RoutingPolicyDraft) {
        policyID = draft.id.trimmingCharacters(in: .whitespacesAndNewlines)
        reliabilityWeight = draft.reliabilityWeight
        headroomWeight = draft.headroomWeight
        latencyWeight = draft.latencyWeight
        costWeight = draft.costWeight
        minimumHeadroomBasisPoints = draft.minimumHeadroomBasisPoints
        maximumErrorRateBasisPoints = draft.maximumErrorRateBasisPoints
        minimumRuntimeSamples = draft.minimumRuntimeSamples
        latencyReferenceMilliseconds = draft.latencyReferenceMilliseconds
        costReferenceMicrounits = draft.costReferenceMicrounits
        costCurrency = draft.costCurrency
            .trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        minimumBalanceMicrounits = draft.minimumBalanceMicrounits
        balanceReferenceMicrounits = draft.balanceReferenceMicrounits
        unknownPenaltyBasisPoints = draft.unknownPenaltyBasisPoints
        requireCost = draft.requireCost
        requireRuntime = draft.requireRuntime
        allowedExecutionClasses = draft.allowedExecutionClasses.sorted()
        allowedPrivacyClasses = draft.allowedPrivacyClasses.sorted()
    }

    var isValid: Bool {
        let builtins = Set(["reliable", "balanced", "economy", "fast", "private"])
        let weights = [reliabilityWeight, headroomWeight, latencyWeight, costWeight]
        let counters = [
            minimumRuntimeSamples, latencyReferenceMilliseconds,
            costReferenceMicrounits, minimumBalanceMicrounits,
            balanceReferenceMicrounits,
        ]
        let executionValues = Set([
            "direct_api", "subscription_cli", "openai_compatible", "self_hosted",
        ])
        let privacyValues = Set(["local_only", "direct_provider", "proxy"])
        return RoutingExecutionConnection.isStableID(policyID)
            && !builtins.contains(policyID)
            && weights.allSatisfy { (0 ... 100).contains($0) }
            && weights.reduce(0, +) == 100
            && (0 ... 10_000).contains(minimumHeadroomBasisPoints)
            && (0 ... 10_000).contains(maximumErrorRateBasisPoints)
            && (0 ... 10_000).contains(unknownPenaltyBasisPoints)
            && counters.allSatisfy { (0 ... 1_000_000_000_000).contains($0) }
            && latencyReferenceMilliseconds > 0
            && costReferenceMicrounits > 0
            && balanceReferenceMicrounits > 0
            && costCurrency.range(
                of: #"^[A-Z][A-Z0-9_]{2,7}$"#,
                options: .regularExpression
            ) != nil
            && !allowedExecutionClasses.isEmpty
            && Set(allowedExecutionClasses).count == allowedExecutionClasses.count
            && Set(allowedExecutionClasses).isSubset(of: executionValues)
            && !allowedPrivacyClasses.isEmpty
            && Set(allowedPrivacyClasses).count == allowedPrivacyClasses.count
            && Set(allowedPrivacyClasses).isSubset(of: privacyValues)
            && (!requireRuntime || minimumRuntimeSamples > 0)
    }
}

struct RoutingPolicyDraft: Sendable, Hashable, Identifiable {
    var id: String
    let isNew: Bool
    var reliabilityWeight: Int
    var headroomWeight: Int
    var latencyWeight: Int
    var costWeight: Int
    var minimumHeadroomBasisPoints: Int
    var maximumErrorRateBasisPoints: Int
    var minimumRuntimeSamples: Int64
    var latencyReferenceMilliseconds: Int64
    var costReferenceMicrounits: Int64
    var costCurrency: String
    var minimumBalanceMicrounits: Int64
    var balanceReferenceMicrounits: Int64
    var unknownPenaltyBasisPoints: Int
    var requireCost: Bool
    var requireRuntime: Bool
    var allowedExecutionClasses: Set<String>
    var allowedPrivacyClasses: Set<String>

    init(
        policy: RoutingCustomPolicy? = nil,
        generatedID: String = "custom_" + UUID().uuidString
            .replacingOccurrences(of: "-", with: "").lowercased()
    ) {
        id = policy?.policyID ?? generatedID
        isNew = policy == nil
        reliabilityWeight = policy?.reliabilityWeight ?? 35
        headroomWeight = policy?.headroomWeight ?? 25
        latencyWeight = policy?.latencyWeight ?? 20
        costWeight = policy?.costWeight ?? 20
        minimumHeadroomBasisPoints = policy?.minimumHeadroomBasisPoints ?? 500
        maximumErrorRateBasisPoints = policy?.maximumErrorRateBasisPoints ?? 2_000
        minimumRuntimeSamples = policy?.minimumRuntimeSamples ?? 5
        latencyReferenceMilliseconds = policy?.latencyReferenceMilliseconds ?? 10_000
        costReferenceMicrounits = policy?.costReferenceMicrounits ?? 1_000
        costCurrency = policy?.costCurrency ?? "USD"
        minimumBalanceMicrounits = policy?.minimumBalanceMicrounits ?? 1_000_000
        balanceReferenceMicrounits = policy?.balanceReferenceMicrounits ?? 20_000_000
        unknownPenaltyBasisPoints = policy?.unknownPenaltyBasisPoints ?? 4_000
        requireCost = policy?.requireCost ?? false
        requireRuntime = policy?.requireRuntime ?? false
        allowedExecutionClasses = Set(policy?.allowedExecutionClasses ?? [
            "direct_api", "subscription_cli", "openai_compatible", "self_hosted",
        ])
        allowedPrivacyClasses = Set(policy?.allowedPrivacyClasses ?? [
            "local_only", "direct_provider", "proxy",
        ])
    }

    var mutationValue: RoutingPolicyMutationValue? {
        let value = RoutingPolicyMutationValue(draft: self)
        return value.isValid ? value : nil
    }

    var canSave: Bool { mutationValue != nil }
}

struct RoutingPolicyMutationRequest: Encodable, Sendable, Hashable {
    let version = 1
    let action = "upsert_policy"
    let expectedRevision: Int64
    let policy: RoutingPolicyMutationValue
}

struct RoutingPolicyRemoveRequest: Encodable, Sendable, Hashable {
    let version = 1
    let action = "remove_policy"
    let expectedRevision: Int64
    let policyID: String

    enum CodingKeys: String, CodingKey {
        case version, action, expectedRevision
        case policyID = "policyId"
    }
}

private struct RoutingPolicyListRequest: Encodable {
    let version = 1
    let action = "list_policies"
}

protocol RoutingPolicyMutationSubmitting: Sendable {
    func loadPolicies(command: ProviderMutationCommand) async
        -> Result<RoutingMutationResponse, ProviderMutationFailure>
    func upsertPolicy(
        _ request: RoutingPolicyMutationRequest,
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure>
    func removePolicy(
        _ request: RoutingPolicyRemoveRequest,
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure>
}

struct RoutingPolicyMutationClient: RoutingPolicyMutationSubmitting, Sendable {
    let limits: ProviderMutationLimits
    private let environment: [String: String]

    init(
        limits: ProviderMutationLimits = .production,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        self.limits = limits
        self.environment = ChildProcessEnvironment.sanitized(environment)
    }

    func loadPolicies(command: ProviderMutationCommand) async
        -> Result<RoutingMutationResponse, ProviderMutationFailure>
    {
        await submit(RoutingPolicyListRequest(), list: true, command: command)
    }

    func upsertPolicy(
        _ request: RoutingPolicyMutationRequest,
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure> {
        guard request.expectedRevision >= 0, request.policy.isValid else {
            return .failure(.invalidResponse)
        }
        return await submit(request, list: false, command: command)
    }

    func removePolicy(
        _ request: RoutingPolicyRemoveRequest,
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure> {
        guard request.expectedRevision >= 0,
              RoutingExecutionConnection.isStableID(request.policyID)
        else { return .failure(.invalidResponse) }
        return await submit(request, list: false, command: command)
    }

    private func submit<Request: Encodable & Sendable>(
        _ request: Request,
        list: Bool,
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure> {
        await Task.detached(priority: .userInitiated) {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            guard let requestData = try? encoder.encode(request),
                  requestData.count <= 256 * 1024
            else { return .failure(.invalidResponse) }
            switch ProviderMutationProcessRunner(environment: environment).run(
                requestData, command: command, limits: limits
            ) {
            case let .failure(failure): return .failure(failure)
            case let .success(data):
                guard let response = try? JSONDecoder().decode(
                    RoutingMutationResponse.self, from: data
                ), response.version == 1,
                   !response.message.isEmpty, response.message.utf8.count <= 256,
                   response.message.unicodeScalars.allSatisfy({
                       $0.value >= 0x20 && $0.value != 0x7f
                   }),
                   response.targetRevision == nil,
                   response.connectionRevision == nil,
                   response.connections == nil,
                   response.preferencesRevision == nil,
                   response.routingPreferences == nil,
                   response.ok == (response.policyDocumentRevision != nil),
                   response.policyDocumentRevision.map({ list ? $0 >= 0 : $0 > 0 }) ?? true
                else { return .failure(.invalidResponse) }
                if list {
                    guard response.ok,
                          let policies = response.customPolicies,
                          policies.count <= 32,
                          policies.allSatisfy(\.isValid),
                          Set(policies.map(\.policyID)).count == policies.count
                    else { return .failure(.invalidResponse) }
                } else if response.customPolicies != nil {
                    return .failure(.invalidResponse)
                }
                return .success(response)
            }
        }.value
    }
}
