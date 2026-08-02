import Foundation
import UsageCore

enum RoutingProxyAction: String, Sendable, Hashable {
    case status, enable, disable, rotate

    var returnsNewToken: Bool { self == .enable || self == .rotate }
}

struct RoutingProxyStatus: Sendable, Hashable {
    let enabled: Bool
    let endpoint: String
    let configurationRevision: Int64
    let restartRequired: Bool
    let bearerToken: String?
}

protocol RoutingProxyManaging: Sendable {
    func run(
        action: RoutingProxyAction,
        command: ProviderMutationCommand
    ) async -> Result<RoutingProxyStatus, ProviderMutationFailure>
}

struct RoutingProxyManagementClient: RoutingProxyManaging, Sendable {
    let limits: ProviderMutationLimits
    private let environment: [String: String]

    init(
        limits: ProviderMutationLimits = .production,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) {
        self.limits = limits
        self.environment = ChildProcessEnvironment.sanitized(environment)
    }

    func run(
        action: RoutingProxyAction,
        command: ProviderMutationCommand
    ) async -> Result<RoutingProxyStatus, ProviderMutationFailure> {
        await Task.detached(priority: .userInitiated) {
            switch ProviderMutationProcessRunner(environment: environment).run(
                Data(), command: command, limits: limits
            ) {
            case let .failure(failure):
                return .failure(failure)
            case let .success(data):
                guard String(data: data, encoding: .utf8) != nil,
                      let wire = try? JSONDecoder().decode(ProxyWire.self, from: data),
                      let status = wire.validated(for: action)
                else { return .failure(.invalidResponse) }
                return .success(status)
            }
        }.value
    }
}

private struct ProxyWire: Decodable {
    let schemaVersion: String
    let enabled: Bool
    let endpoint: String
    let configurationRevision: Int64
    let restartRequired: Bool?
    let bearerToken: String?

    func validated(for action: RoutingProxyAction) -> RoutingProxyStatus? {
        guard schemaVersion == "1.0",
              configurationRevision >= 0,
              validEndpoint(endpoint),
              bearerToken.map(validToken) ?? true,
              action.returnsNewToken == (bearerToken != nil),
              action != .enable || enabled,
              action != .disable || !enabled
        else { return nil }
        return RoutingProxyStatus(
            enabled: enabled,
            endpoint: endpoint,
            configurationRevision: configurationRevision,
            restartRequired: restartRequired ?? false,
            bearerToken: bearerToken
        )
    }

    private func validEndpoint(_ value: String) -> Bool {
        guard value.utf8.count <= 128,
              let components = URLComponents(string: value),
              components.scheme == "http",
              components.host == "127.0.0.1",
              let port = components.port,
              (1...65_535).contains(port),
              components.path == "/v1",
              components.user == nil,
              components.password == nil,
              components.query == nil,
              components.fragment == nil
        else { return false }
        return true
    }

    private func validToken(_ value: String) -> Bool {
        (24...256).contains(value.utf8.count)
            && !value.contains(where: { $0.isWhitespace || $0.isNewline })
            && value.unicodeScalars.allSatisfy {
                $0.value >= 0x21 && $0.value != 0x7f
            }
    }
}
