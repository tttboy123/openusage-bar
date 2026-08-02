import Foundation
import UsageCore

struct RoutingPreferencesValue: Codable, Sendable, Hashable {
    let decisionAPIEnabled: Bool
    let defaultPolicyID: String

    enum CodingKeys: String, CodingKey {
        case decisionAPIEnabled = "decisionApiEnabled"
        case defaultPolicyID = "defaultPolicyId"
    }

    var isValid: Bool {
        RoutingExecutionConnection.isStableID(defaultPolicyID)
    }
}

struct RoutingPreferencesMutationRequest: Encodable, Sendable, Hashable {
    let version = 1
    let action = "set_preferences"
    let expectedRevision: Int64
    let decisionAPIEnabled: Bool
    let defaultPolicyID: String

    enum CodingKeys: String, CodingKey {
        case version, action, expectedRevision
        case decisionAPIEnabled = "decisionApiEnabled"
        case defaultPolicyID = "defaultPolicyId"
    }
}

private struct RoutingPreferencesListRequest: Encodable {
    let version = 1
    let action = "get_preferences"
}

protocol RoutingPreferencesMutationSubmitting: Sendable {
    func loadPreferences(command: ProviderMutationCommand) async
        -> Result<RoutingMutationResponse, ProviderMutationFailure>
    func savePreferences(
        _ request: RoutingPreferencesMutationRequest,
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure>
}

struct RoutingPreferencesMutationClient: RoutingPreferencesMutationSubmitting, Sendable {
    let limits: ProviderMutationLimits
    private let environment: [String: String]

    init(
        limits: ProviderMutationLimits = .production,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        self.limits = limits
        self.environment = ChildProcessEnvironment.sanitized(environment)
    }

    func loadPreferences(command: ProviderMutationCommand) async
        -> Result<RoutingMutationResponse, ProviderMutationFailure>
    {
        await submit(RoutingPreferencesListRequest(), list: true, command: command)
    }

    func savePreferences(
        _ request: RoutingPreferencesMutationRequest,
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure> {
        guard request.expectedRevision >= 0,
              RoutingExecutionConnection.isStableID(request.defaultPolicyID)
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
                  requestData.count <= 16 * 1024
            else { return .failure(.invalidResponse) }
            switch ProviderMutationProcessRunner(environment: environment).run(
                requestData, command: command, limits: limits
            ) {
            case let .failure(failure): return .failure(failure)
            case let .success(data):
                guard String(data: data, encoding: .utf8) != nil,
                      let response = try? JSONDecoder().decode(
                          RoutingMutationResponse.self, from: data
                      ),
                      response.version == 1,
                      !response.message.isEmpty,
                      response.message.utf8.count <= 256,
                      response.message.unicodeScalars.allSatisfy({
                          $0.value >= 0x20 && $0.value != 0x7f
                      }),
                      response.targetRevision == nil,
                      response.connectionRevision == nil,
                      response.connections == nil,
                      response.policyDocumentRevision == nil,
                      response.customPolicies == nil,
                      response.ok == (response.preferencesRevision != nil),
                      response.preferencesRevision.map({ list ? $0 >= 0 : $0 > 0 }) ?? true
                else { return .failure(.invalidResponse) }
                if list {
                    guard response.ok,
                          response.routingPreferences?.isValid == true
                    else { return .failure(.invalidResponse) }
                } else if response.routingPreferences != nil {
                    return .failure(.invalidResponse)
                }
                return .success(response)
            }
        }.value
    }
}
