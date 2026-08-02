import Foundation
import UsageCore

struct RoutingExecutionConnection: Codable, Sendable, Hashable, Identifiable {
    let connectionRef: String
    let providerID: String
    let accountRef: String
    let executionClass: String
    let executionAdapterID: String
    let baseURL: String
    let enabled: Bool
    let models: [String]

    var id: String { connectionRef }

    enum CodingKeys: String, CodingKey {
        case connectionRef, accountRef, executionClass, baseURL, enabled, models
        case providerID = "providerId"
        case executionAdapterID = "executionAdapterId"
    }

    var isValid: Bool {
        let identifiers = [
            connectionRef, providerID, accountRef, executionAdapterID,
        ] + models
        guard identifiers.allSatisfy(Self.isStableID),
              ["direct_api", "subscription_cli", "openai_compatible", "self_hosted"]
                .contains(executionClass),
              !models.isEmpty, models.count <= 256,
              Set(models).count == models.count,
              let components = URLComponents(string: baseURL),
              components.scheme == "https", components.host != nil,
              components.user == nil, components.password == nil,
              components.query == nil, components.fragment == nil,
              baseURL.utf8.count <= 2_048
        else { return false }
        return true
    }

    static func isStableID(_ value: String) -> Bool {
        value.utf8.count <= 128
            && value.range(
                of: #"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"#,
                options: .regularExpression
            ) != nil
    }
}

struct RoutingConnectionMutationRequest: Encodable, Sendable, Hashable {
    let version = 1
    let action = "upsert_connection"
    let expectedRevision: Int64
    let connection: RoutingExecutionConnection
    let secret: String
}

struct RoutingConnectionRemoveRequest: Encodable, Sendable, Hashable {
    let version = 1
    let action = "remove_connection"
    let expectedRevision: Int64
    let connectionRef: String
}

private struct RoutingConnectionListRequest: Encodable {
    let version = 1
    let action = "list_connections"
}

protocol RoutingConnectionMutationSubmitting: Sendable {
    func loadConnections(
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure>

    func upsertConnection(
        _ request: RoutingConnectionMutationRequest,
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure>

    func removeConnection(
        _ request: RoutingConnectionRemoveRequest,
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure>
}

struct RoutingConnectionMutationClient: RoutingConnectionMutationSubmitting, Sendable {
    let limits: ProviderMutationLimits
    private let environment: [String: String]

    init(
        limits: ProviderMutationLimits = .production,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        self.limits = limits
        self.environment = ChildProcessEnvironment.sanitized(environment)
    }

    func loadConnections(
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure> {
        await submit(RoutingConnectionListRequest(), mode: .list, command: command)
    }

    func upsertConnection(
        _ request: RoutingConnectionMutationRequest,
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure> {
        guard request.expectedRevision >= 0,
              request.connection.isValid,
              request.secret.utf8.count <= 65_536,
              !request.secret.contains("\r"), !request.secret.contains("\n")
        else { return .failure(.invalidResponse) }
        return await submit(request, mode: .mutation, command: command)
    }

    func removeConnection(
        _ request: RoutingConnectionRemoveRequest,
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure> {
        guard request.expectedRevision >= 0,
              RoutingExecutionConnection(
                connectionRef: request.connectionRef,
                providerID: "valid", accountRef: "valid",
                executionClass: "openai_compatible",
                executionAdapterID: "valid", baseURL: "https://example.com",
                enabled: false, models: ["valid"]
              ).isValid
        else { return .failure(.invalidResponse) }
        return await submit(request, mode: .mutation, command: command)
    }

    private enum ResponseMode { case list, mutation }

    private func submit<Request: Encodable & Sendable>(
        _ request: Request,
        mode: ResponseMode,
        command: ProviderMutationCommand
    ) async -> Result<RoutingMutationResponse, ProviderMutationFailure> {
        await Task.detached(priority: .userInitiated) {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            guard self.limits.maximumResponseBytes > 0,
                  let requestData = try? encoder.encode(request),
                  requestData.count <= 256 * 1024
            else { return .failure(.invalidResponse) }
            switch ProviderMutationProcessRunner(environment: self.environment).run(
                requestData, command: command, limits: self.limits
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
                      response.targetRevision == nil,
                      response.policyDocumentRevision == nil,
                      response.customPolicies == nil,
                      response.preferencesRevision == nil,
                      response.routingPreferences == nil
                else { return .failure(.invalidResponse) }
                switch mode {
                case .list:
                    guard response.ok,
                          let revision = response.connectionRevision,
                          revision >= 0,
                          let connections = response.connections,
                          connections.count <= 128,
                          connections.allSatisfy(\.isValid),
                          Set(connections.map(\.connectionRef)).count == connections.count
                    else { return .failure(.invalidResponse) }
                case .mutation:
                    guard response.ok == (response.connectionRevision != nil),
                          response.connectionRevision.map({ $0 > 0 }) ?? true,
                          response.connections == nil
                    else { return .failure(.invalidResponse) }
                }
                return .success(response)
            }
        }.value
    }
}
