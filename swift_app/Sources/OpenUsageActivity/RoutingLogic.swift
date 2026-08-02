import Foundation
import Observation
import UsageCore

struct RoutingConnectionDraft: Sendable, Hashable, Identifiable {
    let id: String
    let isNew: Bool
    var providerID: String
    var accountRef: String
    var baseURL: String
    var modelsText: String
    var enabled: Bool
    var secret: String

    init(
        connection: RoutingExecutionConnection? = nil,
        generatedRef: String = "conn_" + UUID().uuidString
            .replacingOccurrences(of: "-", with: "").lowercased()
    ) {
        id = connection?.connectionRef ?? generatedRef
        isNew = connection == nil
        providerID = connection?.providerID ?? ""
        accountRef = connection?.accountRef ?? ""
        baseURL = connection?.baseURL ?? ""
        modelsText = connection?.models.joined(separator: ", ") ?? ""
        enabled = connection?.enabled ?? true
        secret = ""
    }

    var connection: RoutingExecutionConnection {
        let separators = CharacterSet.whitespacesAndNewlines.union(
            CharacterSet(charactersIn: ",;")
        )
        let models = Array(Set(
            modelsText.components(separatedBy: separators).filter { !$0.isEmpty }
        )).sorted()
        return RoutingExecutionConnection(
            connectionRef: id,
            providerID: providerID.trimmingCharacters(in: .whitespacesAndNewlines),
            accountRef: accountRef.trimmingCharacters(in: .whitespacesAndNewlines),
            executionClass: "openai_compatible",
            executionAdapterID: "openai_compatible.direct",
            baseURL: baseURL.trimmingCharacters(in: .whitespacesAndNewlines),
            enabled: enabled,
            models: models
        )
    }

    var canSave: Bool {
        connection.isValid
            && secret.utf8.count <= 65_536
            && !secret.contains("\r") && !secret.contains("\n")
            && (!isNew || !secret.isEmpty)
    }
}

struct RoutingTargetDraft: Sendable, Hashable, Identifiable {
    let id: String
    let isNew: Bool
    var connectionRef: String
    var modelID: String
    var resourceMode: String
    var factAccountRef: String
    var runtimeScopeRef: String
    var balanceCurrency: String
    var costCurrency: String
    var inputCostPerMillion: String
    var outputCostPerMillion: String
    var enabled: Bool
    var regionsText: String
    var privacyClass: String
    var capabilities: Set<String>
    var contextWindowTokens: Int64
    var qualityTier: Int

    init(
        target: RoutingTarget? = nil,
        connection: RoutingExecutionConnection? = nil,
        generatedRef: String = "target_" + UUID().uuidString
            .replacingOccurrences(of: "-", with: "").lowercased()
    ) {
        id = target?.targetID ?? generatedRef
        isNew = target == nil
        connectionRef = target?.connectionRef ?? connection?.connectionRef ?? ""
        modelID = target?.modelID ?? connection?.models.first ?? ""
        resourceMode = target?.resourceMode ?? "quota"
        factAccountRef = target?.factAccountRef ?? connection?.accountRef ?? ""
        runtimeScopeRef = target?.runtimeScopeRef ?? ""
        balanceCurrency = target?.balanceCurrency ?? ""
        costCurrency = target?.costCurrency ?? ""
        inputCostPerMillion = Self.costText(target?.inputCostMicrosPerMillion)
        outputCostPerMillion = Self.costText(target?.outputCostMicrosPerMillion)
        enabled = target?.enabled ?? true
        regionsText = target?.regions.joined(separator: ", ") ?? "global"
        privacyClass = target?.privacyClass ?? "direct_provider"
        capabilities = Set(target?.capabilities ?? ["chat"])
        contextWindowTokens = target?.contextWindowTokens ?? 128_000
        qualityTier = target?.qualityTier ?? 3
    }

    mutating func selectConnection(_ connection: RoutingExecutionConnection) {
        connectionRef = connection.connectionRef
        if !connection.models.contains(modelID) {
            modelID = connection.models.first ?? ""
        }
        factAccountRef = connection.accountRef
    }

    func canSave(connections: [RoutingExecutionConnection]) -> Bool {
        mutationValue(connections: connections) != nil
    }

    func mutationValue(
        connections: [RoutingExecutionConnection]
    ) -> RoutingTargetMutationValue? {
        guard RoutingExecutionConnection.isStableID(id),
              let connection = connections.first(where: {
                  $0.connectionRef == connectionRef
              }),
              connection.models.contains(modelID),
              RoutingExecutionConnection.isStableID(modelID),
              ["quota", "balance"].contains(resourceMode),
              ["local_only", "direct_provider", "proxy"].contains(privacyClass),
              contextWindowTokens > 0,
              contextWindowTokens <= 1_000_000_000_000,
              (0 ... 5).contains(qualityTier)
        else { return nil }

        let factRef = Self.optionalStableID(factAccountRef)
        guard factAccountRef.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                || factRef != nil
        else { return nil }
        let runtimeRef = runtimeScopeRef.trimmingCharacters(in: .whitespacesAndNewlines)
        guard runtimeRef.isEmpty || runtimeRef.range(
            of: #"^anon_[0-9a-f]{16,64}$"#,
            options: .regularExpression
        ) != nil else { return nil }

        let regions = Self.stableIDs(regionsText)
        let capabilityValues = capabilities.sorted()
        guard !regions.isEmpty,
              regions.count <= 50,
              !capabilityValues.isEmpty,
              capabilityValues.count <= 50,
              capabilityValues.allSatisfy(RoutingExecutionConnection.isStableID)
        else { return nil }

        let balance = balanceCurrency
            .trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        guard resourceMode == "quota" || Self.isCurrency(balance) else { return nil }

        let currency = costCurrency
            .trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        let inputText = inputCostPerMillion
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let outputText = outputCostPerMillion
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let hasAnyCost = !currency.isEmpty || !inputText.isEmpty || !outputText.isEmpty
        let inputCost = hasAnyCost ? Self.costMicros(inputText) : nil
        let outputCost = hasAnyCost ? Self.costMicros(outputText) : nil
        guard resourceMode != "balance" || hasAnyCost else { return nil }
        guard !hasAnyCost || (
            Self.isCurrency(currency) && inputCost != nil && outputCost != nil
        ) else { return nil }
        guard resourceMode != "balance" || !hasAnyCost || currency == balance else {
            return nil
        }

        return RoutingTargetMutationValue(
            targetID: id,
            providerID: connection.providerID,
            accountRef: connection.accountRef,
            modelID: modelID,
            connectionRef: connection.connectionRef,
            executionClass: connection.executionClass,
            executionAdapterID: connection.executionAdapterID,
            resourceMode: resourceMode,
            factAccountRef: factRef,
            runtimeScopeRef: runtimeRef.isEmpty ? nil : runtimeRef,
            balanceCurrency: resourceMode == "balance" ? balance : nil,
            costCurrency: hasAnyCost ? currency : nil,
            inputCostMicrosPerMillion: inputCost,
            outputCostMicrosPerMillion: outputCost,
            enabled: enabled,
            regions: regions,
            privacyClass: privacyClass,
            capabilities: capabilityValues,
            contextWindowTokens: contextWindowTokens,
            qualityTier: qualityTier
        )
    }

    private static func optionalStableID(_ value: String) -> String? {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              RoutingExecutionConnection.isStableID(trimmed)
        else { return nil }
        return trimmed
    }

    private static func stableIDs(_ value: String) -> [String] {
        let separators = CharacterSet.whitespacesAndNewlines.union(
            CharacterSet(charactersIn: ",;")
        )
        let values = value.components(separatedBy: separators).filter { !$0.isEmpty }
        guard values.allSatisfy(RoutingExecutionConnection.isStableID) else { return [] }
        return Array(Set(values)).sorted()
    }

    private static func isCurrency(_ value: String) -> Bool {
        value.range(of: #"^[A-Z][A-Z0-9_]{2,7}$"#, options: .regularExpression) != nil
    }

    private static func costMicros(_ value: String) -> Int64? {
        guard value.range(
            of: #"^[0-9]{1,7}(?:\.[0-9]{1,6})?$"#,
            options: .regularExpression
        ) != nil else { return nil }
        let parts = value.split(separator: ".", omittingEmptySubsequences: false)
        guard let whole = Int64(parts[0]), whole <= 1_000_000 else { return nil }
        let fractionText = parts.count == 2
            ? String(parts[1]).padding(toLength: 6, withPad: "0", startingAt: 0)
            : "000000"
        guard let fraction = Int64(fractionText) else { return nil }
        let result = whole * 1_000_000 + fraction
        return result <= 1_000_000_000_000 ? result : nil
    }

    private static func costText(_ value: Int64?) -> String {
        guard let value else { return "" }
        let whole = value / 1_000_000
        let fraction = value % 1_000_000
        guard fraction != 0 else { return String(whole) }
        var suffix = String(format: "%06lld", fraction)
        while suffix.last == "0" { suffix.removeLast() }
        return "\(whole).\(suffix)"
    }
}

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

struct RoutingEvaluationSummary: Sendable, Equatable {
    let sampleCount: Int
    let agreementCount: Int
    let agreementBasisPoints: Int
    let actualRejectedCount: Int
    let meanScoreAdvantage: Int?
    let meanLatencyDeltaMilliseconds: Int64?

    init(shadows: [RoutingShadowEvaluation]) {
        sampleCount = shadows.count
        agreementCount = shadows.filter(\.agreement).count
        agreementBasisPoints = shadows.isEmpty
            ? 0 : agreementCount * 10_000 / shadows.count
        actualRejectedCount = shadows.count { $0.actualState == "rejected" }
        let scores = shadows.compactMap(\.scoreAdvantage)
        meanScoreAdvantage = scores.isEmpty
            ? nil : scores.reduce(0, +) / scores.count
        let latencies = shadows.compactMap(\.estimatedLatencyDeltaMilliseconds)
        meanLatencyDeltaMilliseconds = latencies.isEmpty
            ? nil : latencies.reduce(0, +) / Int64(latencies.count)
    }
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
    private let connectionMutations: any RoutingConnectionMutationSubmitting
    private let policyMutations: any RoutingPolicyMutationSubmitting
    private let preferenceMutations: any RoutingPreferencesMutationSubmitting

    private(set) var health: RoutingHealth?
    private(set) var policies: [RoutingPolicy] = []
    private(set) var targets: [RoutingTarget] = []
    private(set) var history: [RoutingHistoryDecision] = []
    private(set) var decision: RoutingDecision?
    private(set) var shadowHistory: [RoutingShadowEvaluation] = []
    private(set) var shadowResult: RoutingShadowEvaluation?
    private(set) var replayReport: RoutingReplayReport?
    private(set) var connections: [RoutingExecutionConnection] = []
    private(set) var customPolicies: [RoutingCustomPolicy] = []
    private(set) var failure: RoutingFailure?
    private(set) var mutationFailure: RoutingFailure?
    private(set) var evaluationFailure: RoutingFailure?
    private(set) var isLoading = false
    private(set) var isSimulating = false
    private(set) var isEvaluating = false
    private(set) var isMutating = false
    private(set) var isLoadingConnections = false
    private(set) var isLoadingPolicies = false
    private(set) var isLoadingPreferences = false
    private(set) var targetRevision: Int64 = 0
    private(set) var connectionRevision: Int64 = 0
    private(set) var policyDocumentRevision: Int64 = 0
    private(set) var preferencesRevision: Int64 = 0
    private(set) var decisionAPIEnabled = false

    var selectedPolicyID = "reliable"
    var taskKind = RoutingTaskKind.code
    var estimatedInputTokens: Int64 = 12_000
    var maxOutputTokens: Int64 = 4_000
    var minimumContextTokens: Int64 = 16_000
    var requiresReasoning = true
    var requiresTools = true
    var privacy = RoutingPrivacy.directProvider
    var selectedActualTargetID = ""

    var evaluationSummary: RoutingEvaluationSummary {
        RoutingEvaluationSummary(shadows: shadowHistory)
    }

    init(
        client: any RoutingAPIReading = RoutingAPIClient(),
        mutations: any RoutingMutationSubmitting = RoutingMutationClient(),
        connectionMutations: any RoutingConnectionMutationSubmitting =
            RoutingConnectionMutationClient(),
        policyMutations: any RoutingPolicyMutationSubmitting =
            RoutingPolicyMutationClient(),
        preferenceMutations: any RoutingPreferencesMutationSubmitting =
            RoutingPreferencesMutationClient()
    ) {
        self.client = client
        self.mutations = mutations
        self.connectionMutations = connectionMutations
        self.policyMutations = policyMutations
        self.preferenceMutations = preferenceMutations
    }

    func loadPreferences(command: ProviderMutationCommand) async {
        guard !isLoadingPreferences else { return }
        isLoadingPreferences = true
        defer { isLoadingPreferences = false }
        switch await preferenceMutations.loadPreferences(command: command) {
        case let .success(response):
            guard response.ok,
                  let revision = response.preferencesRevision,
                  let preferences = response.routingPreferences
            else {
                mutationFailure = .invalidData
                return
            }
            preferencesRevision = revision
            decisionAPIEnabled = preferences.decisionAPIEnabled
            selectedPolicyID = preferences.defaultPolicyID
        case let .failure(error):
            mutationFailure = Self.failure(for: error)
        }
    }

    func savePreferences(
        decisionAPIEnabled: Bool,
        defaultPolicyID: String,
        command: ProviderMutationCommand
    ) async {
        guard !isMutating,
              RoutingExecutionConnection.isStableID(defaultPolicyID)
        else { return }
        isMutating = true
        mutationFailure = nil
        defer { isMutating = false }
        let result = await preferenceMutations.savePreferences(
            RoutingPreferencesMutationRequest(
                expectedRevision: preferencesRevision,
                decisionAPIEnabled: decisionAPIEnabled,
                defaultPolicyID: defaultPolicyID
            ),
            command: command
        )
        switch result {
        case let .success(response):
            guard response.ok, let revision = response.preferencesRevision else {
                mutationFailure = .invalidData
                return
            }
            preferencesRevision = revision
            self.decisionAPIEnabled = decisionAPIEnabled
            selectedPolicyID = defaultPolicyID
        case let .failure(error):
            mutationFailure = Self.failure(for: error)
        }
    }

    func loadCustomPolicies(command: ProviderMutationCommand) async {
        guard !isLoadingPolicies else { return }
        isLoadingPolicies = true
        mutationFailure = nil
        defer { isLoadingPolicies = false }
        switch await policyMutations.loadPolicies(command: command) {
        case let .success(response):
            guard response.ok,
                  let revision = response.policyDocumentRevision,
                  let policies = response.customPolicies
            else {
                customPolicies = []
                policyDocumentRevision = 0
                mutationFailure = .invalidData
                return
            }
            customPolicies = policies.sorted { $0.policyID < $1.policyID }
            policyDocumentRevision = revision
        case let .failure(error):
            customPolicies = []
            policyDocumentRevision = 0
            mutationFailure = Self.failure(for: error)
        }
    }

    func upsertCustomPolicy(
        _ policy: RoutingPolicyMutationValue,
        command: ProviderMutationCommand
    ) async {
        guard !isMutating, policy.isValid else { return }
        isMutating = true
        mutationFailure = nil
        let result = await policyMutations.upsertPolicy(
            RoutingPolicyMutationRequest(
                expectedRevision: policyDocumentRevision, policy: policy
            ),
            command: command
        )
        isMutating = false
        switch result {
        case let .success(response):
            guard response.ok else {
                mutationFailure = .invalidData
                return
            }
            await loadCustomPolicies(command: command)
            await load()
        case let .failure(error):
            mutationFailure = Self.failure(for: error)
        }
    }

    func removeCustomPolicy(
        _ policyID: String,
        command: ProviderMutationCommand
    ) async {
        guard !isMutating,
              customPolicies.contains(where: { $0.policyID == policyID })
        else { return }
        isMutating = true
        mutationFailure = nil
        let result = await policyMutations.removePolicy(
            RoutingPolicyRemoveRequest(
                expectedRevision: policyDocumentRevision, policyID: policyID
            ),
            command: command
        )
        isMutating = false
        switch result {
        case let .success(response):
            guard response.ok else {
                mutationFailure = .invalidData
                return
            }
            await loadCustomPolicies(command: command)
            await load()
        case let .failure(error):
            mutationFailure = Self.failure(for: error)
        }
    }

    func loadConnections(command: ProviderMutationCommand) async {
        guard !isLoadingConnections else { return }
        isLoadingConnections = true
        mutationFailure = nil
        defer { isLoadingConnections = false }
        switch await connectionMutations.loadConnections(command: command) {
        case let .success(response):
            guard response.ok,
                  let revision = response.connectionRevision,
                  let loaded = response.connections
            else {
                connections = []
                connectionRevision = 0
                mutationFailure = .invalidData
                return
            }
            connections = loaded.sorted { $0.connectionRef < $1.connectionRef }
            connectionRevision = revision
        case let .failure(error):
            connections = []
            connectionRevision = 0
            mutationFailure = Self.failure(for: error)
        }
    }

    func upsertConnection(
        _ connection: RoutingExecutionConnection,
        secret: String,
        command: ProviderMutationCommand
    ) async {
        guard !isMutating, connection.isValid else { return }
        isMutating = true
        mutationFailure = nil
        let result = await connectionMutations.upsertConnection(
            RoutingConnectionMutationRequest(
                expectedRevision: connectionRevision,
                connection: connection,
                secret: secret
            ),
            command: command
        )
        isMutating = false
        switch result {
        case let .success(response):
            guard response.ok else {
                mutationFailure = .invalidData
                return
            }
            await loadConnections(command: command)
        case let .failure(error):
            mutationFailure = Self.failure(for: error)
        }
    }

    func removeConnection(
        _ connectionRef: String,
        command: ProviderMutationCommand
    ) async {
        guard !isMutating,
              connections.contains(where: { $0.connectionRef == connectionRef })
        else { return }
        isMutating = true
        mutationFailure = nil
        let result = await connectionMutations.removeConnection(
            RoutingConnectionRemoveRequest(
                expectedRevision: connectionRevision,
                connectionRef: connectionRef
            ),
            command: command
        )
        isMutating = false
        switch result {
        case let .success(response):
            guard response.ok else {
                mutationFailure = .invalidData
                return
            }
            await loadConnections(command: command)
        case let .failure(error):
            mutationFailure = Self.failure(for: error)
        }
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
            async let loadedShadows = client.shadowHistory(before: nil, limit: 20)
            let values = try await (
                loadedHealth, loadedPolicies, loadedTargets, loadedHistory, loadedShadows
            )
            health = values.0
            decisionAPIEnabled = values.0.decisionAPIEnabled
            preferencesRevision = values.0.preferencesRevision
            policies = values.1
            targets = values.2.targets
            targetRevision = values.2.revision
            history = values.3.decisions
            shadowHistory = values.4.shadows
            selectedPolicyID = values.0.defaultPolicyID
            if !policies.contains(where: { $0.policyID == selectedPolicyID }),
               let first = policies.first {
                selectedPolicyID = first.policyID
            }
            if !targets.contains(where: { $0.targetID == selectedActualTargetID }) {
                selectedActualTargetID = targets.first?.targetID ?? ""
            }
        } catch {
            health = nil
            policies = []
            targets = []
            targetRevision = 0
            history = []
            shadowHistory = []
            selectedActualTargetID = ""
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
            mutationFailure = Self.failure(for: error)
        }
    }

    func upsertTarget(
        _ target: RoutingTargetMutationValue,
        command: ProviderMutationCommand
    ) async {
        guard !isMutating else { return }
        var values = targets.map { RoutingTargetMutationValue(target: $0) }
        if let index = values.firstIndex(where: { $0.targetID == target.targetID }) {
            values[index] = target
        } else {
            guard values.count < 128 else { return }
            values.append(target)
        }
        await replaceTargets(values, command: command)
    }

    func removeTarget(
        _ targetID: String,
        command: ProviderMutationCommand
    ) async {
        guard !isMutating, targets.contains(where: { $0.targetID == targetID }) else {
            return
        }
        await replaceTargets(
            targets.filter { $0.targetID != targetID }
                .map { RoutingTargetMutationValue(target: $0) },
            command: command
        )
    }

    private func replaceTargets(
        _ values: [RoutingTargetMutationValue],
        command: ProviderMutationCommand
    ) async {
        guard !isMutating else { return }
        isMutating = true
        mutationFailure = nil
        defer { isMutating = false }
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
            mutationFailure = Self.failure(for: error)
        }
    }

    func simulate() async {
        guard !isSimulating else { return }
        isSimulating = true
        failure = nil
        defer { isSimulating = false }
        do {
            decision = try await client.simulate(decisionRequest())
        } catch {
            decision = nil
            failure = Self.failure(for: error)
        }
    }

    func recordShadow() async {
        guard !isEvaluating,
              targets.contains(where: { $0.targetID == selectedActualTargetID })
        else { return }
        isEvaluating = true
        evaluationFailure = nil
        defer { isEvaluating = false }
        do {
            let result = try await client.shadow(
                decisionRequest(), actualTargetID: selectedActualTargetID
            )
            shadowResult = result
            shadowHistory.removeAll { $0.shadowID == result.shadowID }
            shadowHistory.insert(result, at: 0)
            if shadowHistory.count > 20 { shadowHistory.removeLast() }
        } catch {
            evaluationFailure = Self.failure(for: error)
        }
    }

    func replay(_ fixture: Data) async {
        guard !isEvaluating else { return }
        isEvaluating = true
        evaluationFailure = nil
        defer { isEvaluating = false }
        do {
            replayReport = try await client.replay(fixture)
        } catch {
            replayReport = nil
            evaluationFailure = Self.failure(for: error)
        }
    }

    private func decisionRequest() -> RoutingDecisionRequest {
        var capabilities = [RoutingCapability.chat]
        if requiresReasoning { capabilities.append(.reasoning) }
        if requiresTools { capabilities.append(.tools) }
        return RoutingDecisionRequest(
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
    }

    private static func failure(for error: Error) -> RoutingFailure {
        guard let error = error as? RoutingAPIClientError else { return .invalidData }
        return switch error {
        case .unavailable, .serverUnavailable: .serviceUnavailable
        case .timedOut: .timedOut
        case .responseTooLarge, .invalidRequest, .invalidResponse, .schemaMismatch: .invalidData
        }
    }

    private static func failure(for error: ProviderMutationFailure) -> RoutingFailure {
        switch error {
        case .timedOut: .timedOut
        case .unavailable, .couldNotLaunch: .serviceUnavailable
        case .responseTooLarge, .invalidResponse: .invalidData
        }
    }
}
