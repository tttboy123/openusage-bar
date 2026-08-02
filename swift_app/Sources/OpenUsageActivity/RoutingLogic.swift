import Foundation
import Observation
import UsageCore

enum RoutingFailure: Sendable, Equatable {
    case serviceUnavailable
    case timedOut
    case invalidData

    var title: String {
        switch self {
        case .serviceUnavailable: AppLocalization.text("Routing service unavailable")
        case .timedOut: AppLocalization.text("Routing request timed out")
        case .invalidData: AppLocalization.text("Routing data could not be verified")
        }
    }
}

enum RoutingTargetReadiness: Sendable, Equatable {
    case ready
    case disabled
    case adapterMissing

    var title: String {
        switch self {
        case .ready: AppLocalization.text("Ready")
        case .disabled: AppLocalization.text("Disabled")
        case .adapterMissing: AppLocalization.text("Execution adapter missing")
        }
    }

    var symbol: String {
        switch self {
        case .ready: "checkmark.circle.fill"
        case .disabled: "pause.circle"
        case .adapterMissing: "exclamationmark.triangle.fill"
        }
    }
}

struct RoutingTargetStatus: Sendable, Equatable {
    let state: RoutingTargetReadiness
    let title: String
}

enum RoutingPresentation {
    static func readiness(_ target: RoutingTarget) -> RoutingTargetStatus {
        let state: RoutingTargetReadiness
        if !target.enabled { state = .disabled }
        else if !target.adapterAvailable { state = .adapterMissing }
        else { state = .ready }
        return RoutingTargetStatus(state: state, title: state.title)
    }

    static func reason(_ code: String) -> String {
        let key = switch code {
        case "target_disabled": "Target is disabled"
        case "adapter_unavailable": "Execution adapter is unavailable"
        case "not_allowed": "Target is not allowed by this policy"
        case "capability_missing": "Required capability is missing"
        case "context_too_small": "Context window is too small"
        case "privacy_incompatible": "Privacy requirement is incompatible"
        case "region_incompatible": "Region requirement is incompatible"
        case "connection_unavailable": "Provider connection is unavailable"
        case "source_unhealthy": "Provider source is unhealthy"
        case "fact_missing": "Usage facts are missing"
        case "fact_stale": "Usage facts are stale"
        case "coverage_partial": "Usage coverage is partial"
        case "quota_reserve_exceeded": "Quota reserve would be exceeded"
        case "balance_reserve_exceeded": "Balance reserve would be exceeded"
        case "error_rate_exceeded": "Recent error rate is too high"
        case "session_budget_exceeded": "Session budget would be exceeded"
        case "cost_unknown": "Cost is unknown"
        case "cost_limit_exceeded": "Cost limit would be exceeded"
        case "healthy_source": "Provider source is healthy"
        case "quota_headroom": "Quota headroom is available"
        case "balance_headroom": "Balance headroom is available"
        case "low_recent_error_rate": "Recent error rate is low"
        case "latency_observed": "Recent latency is available"
        case "cost_known": "Cost is known"
        default: "Routing constraint was not satisfied"
        }
        return AppLocalization.text(key)
    }

    static func policyTitle(_ policyID: String) -> String {
        let key = switch policyID {
        case "reliable": "Reliable"
        case "balanced": "Balanced"
        case "economy": "Economy"
        case "fast": "Fast"
        case "private": "Private"
        default: policyID
        }
        return AppLocalization.text(key)
    }

    static func taskTitle(_ kind: RoutingTaskKind) -> String {
        AppLocalization.text(kind.rawValue.capitalized)
    }
}

@MainActor
@Observable
final class RoutingViewModel {
    private let client: any RoutingAPIReading
    private let mutations: any RoutingMutationSubmitting

    private(set) var health: RoutingHealth?
    private(set) var policies: [RoutingPolicy] = []
    private(set) var targets: [RoutingTarget] = []
    private(set) var history: [RoutingHistoryDecision] = []
    private(set) var decision: RoutingDecision?
    private(set) var failure: RoutingFailure?
    private(set) var mutationFailure: RoutingFailure?
    private(set) var isLoading = false
    private(set) var isSimulating = false
    private(set) var isMutating = false
    private(set) var targetRevision: Int64 = 0

    var selectedPolicyID = "reliable"
    var taskKind = RoutingTaskKind.code
    var estimatedInputTokens: Int64 = 12_000
    var maxOutputTokens: Int64 = 4_000
    var minimumContextTokens: Int64 = 16_000
    var requiresReasoning = true
    var requiresTools = true
    var privacy = RoutingPrivacy.directProvider

    init(
        client: any RoutingAPIReading = RoutingAPIClient(),
        mutations: any RoutingMutationSubmitting = RoutingMutationClient()
    ) {
        self.client = client
        self.mutations = mutations
    }

    func load() async {
        guard !isLoading else { return }
        isLoading = true
        failure = nil
        defer { isLoading = false }
        do {
            async let loadedHealth = client.health()
            async let loadedPolicies = client.policies()
            async let loadedTargets = client.targets()
            async let loadedHistory = client.history(before: nil, limit: 20)
            let values = try await (
                loadedHealth, loadedPolicies, loadedTargets, loadedHistory
            )
            health = values.0
            policies = values.1
            targets = values.2.targets
            targetRevision = values.2.revision
            history = values.3.decisions
            if !policies.contains(where: { $0.policyID == selectedPolicyID }),
               let first = policies.first {
                selectedPolicyID = first.policyID
            }
        } catch {
            health = nil
            policies = []
            targets = []
            targetRevision = 0
            history = []
            failure = Self.failure(for: error)
        }
    }

    func setTargetEnabled(
        _ targetID: String,
        enabled: Bool,
        command: ProviderMutationCommand
    ) async {
        guard !isMutating, targets.contains(where: { $0.targetID == targetID }) else { return }
        isMutating = true
        mutationFailure = nil
        defer { isMutating = false }
        let values = targets.map { target in
            RoutingTargetMutationValue(
                target: target,
                enabled: target.targetID == targetID ? enabled : nil
            )
        }
        let result = await mutations.submit(
            RoutingTargetMutationRequest(
                expectedRevision: targetRevision,
                targets: values
            ),
            command: command
        )
        switch result {
        case let .success(response):
            guard response.ok else {
                mutationFailure = .invalidData
                return
            }
            await load()
        case let .failure(error):
            mutationFailure = switch error {
            case .timedOut: .timedOut
            case .unavailable, .couldNotLaunch: .serviceUnavailable
            case .responseTooLarge, .invalidResponse: .invalidData
            }
        }
    }

    func simulate() async {
        guard !isSimulating else { return }
        isSimulating = true
        failure = nil
        defer { isSimulating = false }
        var capabilities = [RoutingCapability.chat]
        if requiresReasoning { capabilities.append(.reasoning) }
        if requiresTools { capabilities.append(.tools) }
        let request = RoutingDecisionRequest(
            policyID: selectedPolicyID,
            task: RoutingTask(
                kind: taskKind,
                requiredCapabilities: capabilities,
                estimatedInputTokens: estimatedInputTokens,
                maxOutputTokens: maxOutputTokens,
                minimumContextWindowTokens: minimumContextTokens,
                privacy: privacy,
                regions: ["global"]
            )
        )
        do {
            decision = try await client.simulate(request)
        } catch {
            decision = nil
            failure = Self.failure(for: error)
        }
    }

    private static func failure(for error: Error) -> RoutingFailure {
        guard let error = error as? RoutingAPIClientError else { return .invalidData }
        return switch error {
        case .unavailable, .serverUnavailable: .serviceUnavailable
        case .timedOut: .timedOut
        case .responseTooLarge, .invalidRequest, .invalidResponse, .schemaMismatch: .invalidData
        }
    }
}
