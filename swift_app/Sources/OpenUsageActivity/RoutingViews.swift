import AppKit
import SwiftUI
import UniformTypeIdentifiers
import UsageCore

struct RoutingPage: View {
    @State private var model = RoutingViewModel()
    @State private var localMutationFailure: String?
    @State private var editingConnection: RoutingConnectionDraft?
    @State private var importingProvider: RoutingProviderExecutionImportDraft?
    @State private var editingTarget: RoutingTargetDraft?
    @State private var editingPolicy: RoutingPolicyDraft?
    @State private var isImportingReplay = false
    @State private var localEvaluationFailure: String?
    @State private var isShowingProxyToken = false
    @State private var isProxyTokenRevealed = false
    @State private var proxyTokenCopied = false

    var body: some View {
        ScrollView(.vertical) {
            VStack(alignment: .leading, spacing: 24) {
                header
                if let failure = model.failure, model.health == nil {
                    ContentUnavailableView(
                        failure.title,
                        systemImage: "arrow.triangle.branch",
                        description: Text("The local Decision API did not return verified routing data.")
                    )
                    .frame(maxWidth: .infinity, minHeight: 240)
                    connections
                    customPolicies
                } else {
                    if let failure = model.mutationFailure?.title ?? localMutationFailure {
                        Label(failure, systemImage: "exclamationmark.triangle.fill")
                            .font(.callout)
                            .foregroundStyle(.orange)
                            .accessibilityLabel(failure)
                    }
                    overview
                    executionProxy
                    connections
                    targets
                    customPolicies
                    dryRun
                    decisionResult
                    evaluation
                    history
                }
            }
            .frame(maxWidth: 920, alignment: .leading)
            .padding(.horizontal, 32)
            .padding(.vertical, 28)
        }
        .background(.background)
        .task { await refreshData() }
        .sheet(item: $editingConnection) { draft in
            RoutingConnectionEditor(
                draft: draft,
                isSaving: model.isMutating,
                onSave: saveConnection,
                onRemove: draft.isNew ? nil : { connectionRef in
                    removeConnection(connectionRef)
                }
            )
        }
        .sheet(item: $importingProvider) { draft in
            RoutingProviderExecutionImportEditor(
                draft: draft,
                isSaving: model.isMutating,
                onImport: importProviderExecutionConnection
            )
        }
        .sheet(item: $editingTarget) { draft in
            RoutingTargetEditor(
                draft: draft,
                connections: model.connections,
                isSaving: model.isMutating,
                onSave: saveTarget,
                onRemove: draft.isNew ? nil : { targetID in
                    removeTarget(targetID)
                }
            )
        }
        .sheet(item: $editingPolicy) { draft in
            RoutingPolicyEditor(
                draft: draft,
                isSaving: model.isMutating,
                onSave: savePolicy,
                onRemove: draft.isNew || draft.id == model.selectedPolicyID ? nil : { policyID in
                    removePolicy(policyID)
                }
            )
        }
        .sheet(isPresented: $isShowingProxyToken, onDismiss: clearProxyToken) {
            proxyTokenSheet
        }
        .fileImporter(
            isPresented: $isImportingReplay,
            allowedContentTypes: [.json],
            allowsMultipleSelection: false
        ) { result in
            switch result {
            case let .success(urls):
                guard let url = urls.first else {
                    localEvaluationFailure = AppLocalization.text("Replay file is unavailable")
                    return
                }
                importReplay(url)
            case .failure:
                localEvaluationFailure = AppLocalization.text("Replay file could not be read")
            }
        }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Routing").font(.largeTitle.weight(.semibold))
                Text("Choose an account and model from verified local resource facts")
                    .font(.callout).foregroundStyle(.secondary)
            }
            Spacer()
            Button("Refresh", systemImage: "arrow.clockwise") {
                Task { await refreshData() }
            }
            .disabled(model.isLoading)
            .keyboardShortcut("r", modifiers: [.command])
            .accessibilityHint("Reload routing health, policies, targets, and history")
        }
    }

    private var connections: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                sectionTitle(
                    "Execution connections",
                    detail: "Inference credentials stay in Keychain"
                )
                Spacer()
                if !model.providerExecutionTemplates.isEmpty {
                    Menu("Reuse Provider Center", systemImage: "key.horizontal") {
                        ForEach(model.providerExecutionTemplates) { template in
                            Button {
                                importingProvider = .init(template: template)
                            } label: {
                                Label(
                                    template.displayName,
                                    systemImage: template.credentialAvailable
                                        ? "key.fill" : "key.slash"
                                )
                            }
                            .disabled(!template.credentialAvailable)
                        }
                    }
                    .menuStyle(.button)
                    .help("Reuse an eligible inference key already managed in Provider Center")
                }
                Button("Add connection", systemImage: "plus") {
                    editingConnection = RoutingConnectionDraft()
                }
                .buttonStyle(.borderedProminent)
            }
            if model.connections.isEmpty {
                ContentUnavailableView(
                    "No execution connections",
                    systemImage: "network.slash",
                    description: Text("Add an explicit inference endpoint before creating routing targets.")
                )
                .frame(maxWidth: .infinity, minHeight: 150)
                .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 12))
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(model.connections.enumerated()), id: \.element.id) { index, item in
                        connectionRow(item)
                        if index < model.connections.count - 1 {
                            Divider().padding(.leading, 42)
                        }
                    }
                }
                .padding(.horizontal, 14)
                .background(.quaternary.opacity(0.28), in: RoundedRectangle(cornerRadius: 12))
            }
        }
    }

    private func connectionRow(_ connection: RoutingExecutionConnection) -> some View {
        HStack(spacing: 12) {
            Image(systemName: connection.enabled ? "network" : "pause.circle")
                .foregroundStyle(connection.enabled ? .green : .secondary)
                .frame(width: 22)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(DisplayText.provider(connection.providerID))
                    .font(.body.weight(.medium))
                Text(connection.accountRef)
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 3) {
                Text(URLComponents(string: connection.baseURL)?.host ?? connection.baseURL)
                    .font(.callout.monospaced()).lineLimit(1)
                Text(AppLocalization.format(
                    "%lld models", Int64(connection.models.count)
                ))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Button("Edit") {
                editingConnection = RoutingConnectionDraft(connection: connection)
            }
            .buttonStyle(.borderedProminent)
            .tint(.indigo)
            .controlSize(.small)
            .accessibilityLabel(AppLocalization.format(
                "Edit %@", DisplayText.provider(connection.providerID)
            ))
        }
        .padding(.vertical, 12)
        .accessibilityElement(children: .combine)
    }

    private var overview: some View {
        GroupBox {
            HStack(alignment: .center, spacing: 24) {
                Toggle(isOn: Binding(
                    get: { model.decisionAPIEnabled },
                    set: { savePreferences(enabled: $0, policyID: model.selectedPolicyID) }
                )) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Decision API").font(.headline)
                        Text(decisionAPIStatus)
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                .toggleStyle(.switch)
                .disabled(model.isMutating || model.health == nil)
                .help("Enable or disable local route decisions")
                .accessibilityLabel("Decision API")
                .accessibilityValue(decisionAPIStatus)
                Spacer()
                VStack(alignment: .leading, spacing: 3) {
                    Text("Default policy").font(.caption).foregroundStyle(.secondary)
                    Picker("Default policy", selection: Binding(
                        get: { model.selectedPolicyID },
                        set: { savePreferences(
                            enabled: model.decisionAPIEnabled, policyID: $0
                        ) }
                    )) {
                        ForEach(model.policies) { policy in
                            Text(RoutingPresentation.policyTitle(policy.policyID))
                                .tag(policy.policyID)
                        }
                    }
                    .labelsHidden()
                    .frame(width: 180)
                    .disabled(model.isMutating || model.policies.isEmpty)
                    .help("Choose the policy used by automatic routing")
                }
                VStack(alignment: .trailing, spacing: 2) {
                    Text("Configured targets").font(.caption).foregroundStyle(.secondary)
                    Text("\(model.health?.targetCount ?? model.targets.count)")
                        .font(.title2.monospacedDigit().weight(.semibold))
                }
                VStack(alignment: .trailing, spacing: 2) {
                    Text("Evidence revision").font(.caption).foregroundStyle(.secondary)
                    Text("\(model.health?.routingRevision ?? 0)")
                        .font(.title2.monospacedDigit().weight(.semibold))
                }
            }
            .padding(6)
        }
    }

    private var executionProxy: some View {
        GroupBox {
            HStack(alignment: .center, spacing: 16) {
                Image(systemName: "arrow.triangle.branch")
                    .font(.title2)
                    .foregroundStyle(model.proxyStatus?.enabled == true ? .indigo : .secondary)
                    .frame(width: 32, height: 32)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 8) {
                        Text("OpenAI-compatible chat proxy")
                            .font(.headline)
                        Text(proxyConfigurationLabel)
                            .font(.caption.weight(.medium))
                            .foregroundStyle(model.proxyStatus?.enabled == true ? .indigo : .secondary)
                            .padding(.horizontal, 7)
                            .padding(.vertical, 2)
                            .background(
                                (model.proxyStatus?.enabled == true ? Color.indigo : Color.secondary)
                                    .opacity(0.12),
                                in: Capsule()
                            )
                    }
                    Text("Optional local execution endpoint for OpenAI-compatible clients")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    if let endpoint = model.proxyStatus?.endpoint {
                        Text(endpoint)
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                    if model.proxyStatus?.restartRequired == true {
                        Label("Relaunch OpenUsage Bar to apply this change", systemImage: "arrow.clockwise")
                            .font(.caption)
                            .foregroundStyle(.orange)
                    } else if !model.decisionAPIEnabled {
                        Text("Enable Decision API before enabling the chat proxy")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer(minLength: 16)
                if model.proxyStatus?.enabled == true {
                    Button("Rotate token") {
                        mutateProxy(.rotate)
                    }
                    .buttonStyle(.bordered)
                    Button("Disable proxy") {
                        mutateProxy(.disable)
                    }
                    .buttonStyle(.bordered)
                } else {
                    Button("Enable proxy", systemImage: "lock.open") {
                        mutateProxy(.enable)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.indigo)
                    .disabled(!model.decisionAPIEnabled || model.proxyStatus == nil)
                }
            }
            .padding(6)
        }
        .disabled(model.isManagingProxy)
        .accessibilityElement(children: .contain)
    }

    private var proxyTokenSheet: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Save your proxy token")
                        .font(.title2.weight(.semibold))
                    Text("This token is shown once and cannot be retrieved later.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: "key.fill")
                    .font(.title2)
                    .foregroundStyle(.indigo)
                    .accessibilityHidden(true)
            }
            HStack(spacing: 10) {
                Text(isProxyTokenRevealed
                     ? (model.oneTimeProxyToken ?? "")
                     : String(repeating: "•", count: 28))
                    .font(.body.monospaced())
                    .lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 10)
                    .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 8))
                Button(isProxyTokenRevealed ? "Hide" : "Reveal") {
                    isProxyTokenRevealed.toggle()
                }
                .buttonStyle(.bordered)
            }
            Text("Clipboard clears automatically after one minute.")
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack {
                if proxyTokenCopied {
                    Label("Copied", systemImage: "checkmark.circle.fill")
                        .font(.callout)
                        .foregroundStyle(.green)
                }
                Spacer()
                Button("Done") { isShowingProxyToken = false }
                    .keyboardShortcut(.cancelAction)
                Button("Copy token", systemImage: "doc.on.doc") {
                    copyProxyToken()
                }
                .buttonStyle(.borderedProminent)
                .tint(.indigo)
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(24)
        .frame(width: 560)
    }

    private var proxyConfigurationLabel: String {
        guard let status = model.proxyStatus else {
            return AppLocalization.text("Unavailable")
        }
        return AppLocalization.text(status.enabled ? "Configured" : "Off")
    }

    private var targets: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                sectionTitle(
                    "Routing targets",
                    detail: "Only explicit, execution-capable targets are eligible"
                )
                Spacer()
                Button("Add target", systemImage: "plus") {
                    guard let connection = model.connections.first(where: \.enabled)
                            ?? model.connections.first
                    else { return }
                    editingTarget = RoutingTargetDraft(connection: connection)
                }
                .buttonStyle(.borderedProminent)
                .disabled(model.connections.isEmpty)
            }
            if model.targets.isEmpty {
                ContentUnavailableView(
                    "No routing targets",
                    systemImage: "point.3.connected.trianglepath.dotted",
                    description: Text("Add an explicit target from Provider Center. Discovered accounts are never enabled automatically.")
                )
                .frame(maxWidth: .infinity, minHeight: 160)
                .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 12))
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(model.targets.enumerated()), id: \.element.id) { index, target in
                        targetRow(target)
                        if index < model.targets.count - 1 { Divider().padding(.leading, 42) }
                    }
                }
                .padding(.horizontal, 14)
                .background(.quaternary.opacity(0.28), in: RoundedRectangle(cornerRadius: 12))
            }
        }
    }

    private func targetRow(_ target: RoutingTarget) -> some View {
        let readiness = RoutingPresentation.readiness(target)
        return HStack(spacing: 12) {
            Image(systemName: readiness.state.symbol)
                .foregroundStyle(color(for: readiness.state))
                .frame(width: 22)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(target.modelID).font(.body.weight(.medium))
                Text("\(DisplayText.provider(target.providerID)) · \(target.accountRef)")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 3) {
                Text(readiness.title).font(.callout.weight(.medium))
                    .foregroundStyle(color(for: readiness.state))
                Text(target.capabilities.joined(separator: " · "))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Button(AppLocalization.text(target.enabled ? "Disable" : "Enable")) {
                mutate(target, enabled: !target.enabled)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .disabled(model.isMutating)
            .accessibilityLabel(
                "\(target.enabled ? AppLocalization.text("Disable") : AppLocalization.text("Enable")) \(target.modelID)"
            )
            Button("Edit") {
                editingTarget = RoutingTargetDraft(target: target)
            }
            .buttonStyle(.borderedProminent)
            .tint(.indigo)
            .controlSize(.small)
            .disabled(model.isMutating)
            .accessibilityLabel(AppLocalization.format("Edit %@", target.modelID))
        }
        .padding(.vertical, 12)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(target.modelID), \(DisplayText.provider(target.providerID)), \(readiness.title)")
    }

    private var customPolicies: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                sectionTitle(
                    "Custom policies",
                    detail: "Built-in safety remains mandatory"
                )
                Spacer()
                Button("Add policy", systemImage: "plus") {
                    editingPolicy = RoutingPolicyDraft()
                }
                .buttonStyle(.borderedProminent)
            }
            if model.customPolicies.isEmpty {
                Text("Use a built-in policy or add a bounded scoring profile.")
                    .font(.callout).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(18)
                    .background(
                        .quaternary.opacity(0.28),
                        in: RoundedRectangle(cornerRadius: 12)
                    )
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(model.customPolicies.enumerated()), id: \.element.id) {
                        index, policy in
                        HStack(spacing: 12) {
                            Image(systemName: "slider.horizontal.3")
                                .foregroundStyle(.secondary)
                                .frame(width: 22)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(policy.policyID).font(.body.weight(.medium))
                                Text(AppLocalization.format(
                                    "Revision %lld", policy.policyRevision
                                ))
                                .font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text(AppLocalization.format(
                                "R %lld · H %lld · L %lld · C %lld",
                                Int64(policy.reliabilityWeight),
                                Int64(policy.headroomWeight),
                                Int64(policy.latencyWeight),
                                Int64(policy.costWeight)
                            ))
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                            Button("Edit") {
                                editingPolicy = RoutingPolicyDraft(policy: policy)
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(.indigo)
                            .controlSize(.small)
                        }
                        .padding(.vertical, 12)
                        if index < model.customPolicies.count - 1 {
                            Divider().padding(.leading, 42)
                        }
                    }
                }
                .padding(.horizontal, 14)
                .background(
                    .quaternary.opacity(0.28),
                    in: RoundedRectangle(cornerRadius: 12)
                )
            }
        }
    }

    private var dryRun: some View {
        VStack(alignment: .leading, spacing: 12) {
            sectionTitle("Dry Run", detail: "Evaluate metadata only; no model request is sent")
            GroupBox {
                Grid(alignment: .leading, horizontalSpacing: 22, verticalSpacing: 14) {
                    GridRow {
                        Text("Policy").foregroundStyle(.secondary)
                        Picker("Policy", selection: $model.selectedPolicyID) {
                            ForEach(model.policies) { policy in
                                Text(RoutingPresentation.policyTitle(policy.policyID))
                                    .tag(policy.policyID)
                            }
                        }
                        .labelsHidden()
                        .frame(maxWidth: 260, alignment: .leading)

                        Text("Task").foregroundStyle(.secondary)
                        Picker("Task", selection: $model.taskKind) {
                            ForEach(RoutingTaskKind.allCases, id: \.self) { kind in
                                Text(RoutingPresentation.taskTitle(kind)).tag(kind)
                            }
                        }
                        .labelsHidden()
                        .frame(maxWidth: 220, alignment: .leading)
                    }
                    GridRow {
                        Text("Input Token estimate").foregroundStyle(.secondary)
                        boundedNumberField("Input Token estimate", value: $model.estimatedInputTokens)
                        Text("Maximum output").foregroundStyle(.secondary)
                        boundedNumberField("Maximum output", value: $model.maxOutputTokens)
                    }
                    GridRow {
                        Text("Minimum context").foregroundStyle(.secondary)
                        boundedNumberField("Minimum context", value: $model.minimumContextTokens)
                        Text("Capabilities").foregroundStyle(.secondary)
                        HStack(spacing: 14) {
                            Toggle("Reasoning", isOn: $model.requiresReasoning)
                            Toggle("Tools", isOn: $model.requiresTools)
                        }
                        .toggleStyle(.checkbox)
                    }
                }
                .padding(6)

                HStack {
                    Text("Prompts, messages, tools, headers and credentials are not accepted by this API.")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button("Run Dry Run", systemImage: "play.fill") {
                        Task { await model.simulate() }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(
                        model.isSimulating || model.policies.isEmpty
                            || !model.decisionAPIEnabled
                    )
                    .keyboardShortcut(.return, modifiers: [.command])
                }
                .padding(.top, 12)
            }
        }
    }

    @ViewBuilder private var decisionResult: some View {
        if let decision = model.decision {
            VStack(alignment: .leading, spacing: 12) {
                sectionTitle("Decision", detail: "Simulation · Data revision \(decision.facts.dataRevision)")
                GroupBox {
                    if let selected = decision.selected {
                        decisionSelection(selected)
                    } else {
                        Label("No eligible route", systemImage: "xmark.circle.fill")
                            .font(.headline).foregroundStyle(.orange)
                    }
                    if !decision.alternatives.isEmpty {
                        Divider().padding(.vertical, 8)
                        Text("Alternatives").font(.subheadline.weight(.semibold))
                        ForEach(decision.alternatives) { candidate in
                            decisionCandidate(candidate)
                        }
                    }
                    if !decision.rejected.isEmpty {
                        Divider().padding(.vertical, 8)
                        Text("Not eligible").font(.subheadline.weight(.semibold))
                        ForEach(decision.rejected) { rejected in
                            HStack(alignment: .top) {
                                Text(rejected.targetID).font(.callout.monospaced())
                                Spacer()
                                Text(rejected.reasonCodes.map(RoutingPresentation.reason).joined(separator: ", "))
                                    .font(.callout).foregroundStyle(.secondary)
                                    .multilineTextAlignment(.trailing)
                            }
                            .padding(.vertical, 3)
                            .accessibilityElement(children: .combine)
                        }
                    }
                }
            }
        }
    }

    private func decisionSelection(_ selected: RoutingScoredTarget) -> some View {
        HStack {
            Label("Selected target", systemImage: "checkmark.circle.fill")
                .font(.headline).foregroundStyle(.green)
            Text(selected.targetID).font(.headline.monospaced())
            Spacer()
            Text("Score \(selected.score)").font(.headline.monospacedDigit())
        }
        .accessibilityElement(children: .combine)
    }

    private func decisionCandidate(_ candidate: RoutingScoredTarget) -> some View {
        HStack {
            Text(candidate.targetID).font(.callout.monospaced())
            Spacer()
            Text("\(candidate.score)").font(.callout.monospacedDigit())
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }

    private var evaluation: some View {
        let summary = model.evaluationSummary
        return VStack(alignment: .leading, spacing: 12) {
            sectionTitle(
                "Policy evaluation",
                detail: "Compare actual choices with policy recommendations"
            )
            GroupBox {
                HStack(alignment: .top, spacing: 28) {
                    evaluationMetric(
                        "Comparisons", value: "\(summary.sampleCount)"
                    )
                    evaluationMetric(
                        "Agreement", value: basisPointPercent(summary.agreementBasisPoints)
                    )
                    evaluationMetric(
                        "Actual rejected", value: "\(summary.actualRejectedCount)"
                    )
                    evaluationMetric(
                        "Mean score advantage",
                        value: signed(summary.meanScoreAdvantage)
                    )
                    Spacer(minLength: 0)
                }
                .padding(.vertical, 4)

                Divider().padding(.vertical, 10)

                HStack(spacing: 12) {
                    Picker("Actual target", selection: $model.selectedActualTargetID) {
                        ForEach(model.targets) { target in
                            Text("\(target.modelID) · \(target.accountRef)")
                                .tag(target.targetID)
                        }
                    }
                    .frame(maxWidth: 300)
                    .disabled(model.targets.isEmpty || model.isEvaluating)
                    Button("Record Shadow", systemImage: "point.3.connected.trianglepath.dotted") {
                        Task { await model.recordShadow() }
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.indigo)
                    .disabled(
                        model.targets.isEmpty || model.isEvaluating
                            || !model.decisionAPIEnabled
                    )
                    .help("Compare this actual target with the current policy without sending a model request")
                    Button("Import Replay", systemImage: "doc.badge.arrow.up") {
                        localEvaluationFailure = nil
                        isImportingReplay = true
                    }
                    .buttonStyle(.bordered)
                    .disabled(model.isEvaluating || !model.decisionAPIEnabled)
                    Spacer()
                }

                Text("Shadow stores only target IDs, scores, reason codes and fact revisions. Replay reads a frozen local JSON fixture and writes no evidence.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.top, 8)

                if let failure = model.evaluationFailure?.title ?? localEvaluationFailure {
                    Label(failure, systemImage: "exclamationmark.triangle.fill")
                        .font(.callout)
                        .foregroundStyle(.orange)
                        .padding(.top, 8)
                }
            }

            if model.shadowHistory.isEmpty {
                Text("No policy comparisons have been recorded yet.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(18)
                    .background(
                        .quaternary.opacity(0.28),
                        in: RoundedRectangle(cornerRadius: 12)
                    )
            } else {
                VStack(spacing: 0) {
                    ForEach(
                        Array(model.shadowHistory.prefix(6).enumerated()),
                        id: \.element.id
                    ) { index, item in
                        shadowRow(item)
                        if index < min(model.shadowHistory.count, 6) - 1 {
                            Divider().padding(.leading, 38)
                        }
                    }
                }
                .padding(.horizontal, 14)
                .background(
                    .quaternary.opacity(0.28),
                    in: RoundedRectangle(cornerRadius: 12)
                )
            }

            if let report = model.replayReport {
                replayReport(report)
            }
        }
    }

    private func evaluationMetric(_ title: LocalizedStringKey, value: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.title3.monospacedDigit().weight(.semibold))
        }
        .accessibilityElement(children: .combine)
    }

    private func shadowRow(_ item: RoutingShadowEvaluation) -> some View {
        HStack(spacing: 12) {
            Image(systemName: item.agreement
                  ? "checkmark.circle.fill"
                  : item.actualState == "rejected"
                    ? "exclamationmark.triangle.fill" : "arrow.triangle.branch")
                .foregroundStyle(item.agreement ? .green : .orange)
                .frame(width: 22)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text("\(item.actualTargetID) → \(item.recommendedTargetID ?? AppLocalization.text("No eligible route"))")
                    .font(.callout.weight(.medium))
                    .lineLimit(1)
                Text(RoutingPresentation.policyTitle(item.policy.policyID))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(AppLocalization.text(item.agreement ? "Agreement" : "Different choice"))
                    .font(.callout.weight(.medium))
                Text(DateText.display(item.createdAt))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 10)
        .accessibilityElement(children: .combine)
    }

    private func replayReport(_ report: RoutingReplayReport) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text("Replay report").font(.title2.weight(.semibold))
                Text("\(RoutingPresentation.policyTitle(report.policy.policyID)) · \(AppLocalization.text("Frozen facts"))")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            GroupBox {
                HStack(alignment: .top, spacing: 28) {
                    evaluationMetric("Cases", value: "\(report.summary.caseCount)")
                    evaluationMetric(
                        "Agreement",
                        value: basisPointPercent(report.summary.agreementBasisPoints)
                    )
                    evaluationMetric(
                        "No route", value: "\(report.summary.noRouteCount)"
                    )
                    evaluationMetric(
                        "Actual rejected",
                        value: "\(report.summary.actualRejectedCount)"
                    )
                    Spacer(minLength: 0)
                }
                if !report.summary.costDeltas.isEmpty {
                    Divider().padding(.vertical, 8)
                    ForEach(report.summary.costDeltas, id: \.currency) { delta in
                        HStack {
                            Text(delta.currency).font(.callout.monospaced())
                            Spacer()
                            Text(AppLocalization.format(
                                "%lld μ total · %lld μ mean",
                                delta.totalDeltaMicrounits,
                                delta.meanDeltaMicrounits
                            ))
                            .font(.callout.monospacedDigit())
                            .foregroundStyle(.secondary)
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
            }
        }
    }

    private func basisPointPercent(_ value: Int) -> String {
        let whole = value / 100
        let tenth = value % 100 / 10
        return tenth == 0 ? "\(whole)%" : "\(whole).\(tenth)%"
    }

    private func signed<T: BinaryInteger>(_ value: T?) -> String {
        guard let value else { return "—" }
        return value > 0 ? "+\(value)" : "\(value)"
    }

    private var history: some View {
        VStack(alignment: .leading, spacing: 12) {
            sectionTitle("Recent decisions", detail: "Content-free evidence retained for seven days")
            if model.history.isEmpty {
                Text("No routing decisions have been recorded yet.")
                    .font(.callout).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(18)
                    .background(.quaternary.opacity(0.28), in: RoundedRectangle(cornerRadius: 12))
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(model.history.enumerated()), id: \.element.id) { index, item in
                        HStack(spacing: 14) {
                            Image(systemName: item.selected == nil
                                  ? "xmark.circle" : "checkmark.circle")
                                .foregroundStyle(item.selected == nil ? .orange : .secondary)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(item.selected?.targetID ?? AppLocalization.text("No eligible route"))
                                    .font(.callout.weight(.medium)).lineLimit(1)
                                Text(RoutingPresentation.policyTitle(item.policy.policyID))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text(DateText.display(item.generatedAt))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        .padding(.vertical, 10)
                        .accessibilityElement(children: .combine)
                        if index < model.history.count - 1 { Divider().padding(.leading, 38) }
                    }
                }
                .padding(.horizontal, 14)
                .background(.quaternary.opacity(0.28), in: RoundedRectangle(cornerRadius: 12))
            }
        }
    }

    private func sectionTitle(_ title: LocalizedStringKey, detail: LocalizedStringKey) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(title).font(.title2.weight(.semibold))
            Text(detail).font(.callout).foregroundStyle(.secondary)
        }
    }

    private func boundedNumberField(
        _ label: LocalizedStringKey, value: Binding<Int64>
    ) -> some View {
        TextField(label, value: value, format: .number.grouping(.never))
            .textFieldStyle(.roundedBorder)
            .frame(maxWidth: 180)
            .onChange(of: value.wrappedValue) { _, newValue in
                value.wrappedValue = min(1_000_000_000_000, max(0, newValue))
            }
    }

    private func color(for readiness: RoutingTargetReadiness) -> Color {
        switch readiness {
        case .ready: .green
        case .disabled: .secondary
        case .adapterMissing: .orange
        }
    }

    private func mutate(_ target: RoutingTarget, enabled: Bool) {
        localMutationFailure = nil
        guard let command = routingCommand() else {
            localMutationFailure = AppLocalization.text("Routing target update unavailable")
            return
        }
        Task {
            await model.setTargetEnabled(
                target.targetID,
                enabled: enabled,
                command: command
            )
        }
    }

    private func refreshData() async {
        await model.load()
        if let command = routingCommand() {
            await model.loadPreferences(command: command)
            await model.loadConnections(command: command)
            await model.loadProviderExecutionTemplates(command: command)
            await model.loadCustomPolicies(command: command)
        }
        if let command = proxyCommand(.status) {
            await model.loadProxy(command: command)
        }
    }

    private func importProviderExecutionConnection(
        _ draft: RoutingProviderExecutionImportDraft
    ) {
        localMutationFailure = nil
        guard draft.canImport, let command = routingCommand() else {
            localMutationFailure = AppLocalization.text(
                "Provider credential import is unavailable"
            )
            return
        }
        Task {
            await model.importProviderExecutionConnection(
                providerID: draft.template.providerID,
                connectionRef: draft.id,
                models: draft.models,
                createTargets: draft.createsRoutingTargets,
                command: command
            )
        }
    }

    private func mutateProxy(_ action: RoutingProxyAction) {
        localMutationFailure = nil
        guard let command = proxyCommand(action) else {
            localMutationFailure = AppLocalization.text("Proxy management unavailable")
            return
        }
        Task {
            await model.mutateProxy(action: action, command: command)
            if model.oneTimeProxyToken != nil {
                isProxyTokenRevealed = false
                proxyTokenCopied = false
                isShowingProxyToken = true
            }
        }
    }

    private func copyProxyToken() {
        guard let token = model.oneTimeProxyToken else { return }
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        if pasteboard.setString(token, forType: .string) {
            proxyTokenCopied = true
            let changeCount = pasteboard.changeCount
            DispatchQueue.main.asyncAfter(deadline: .now() + 60) {
                guard pasteboard.changeCount == changeCount else { return }
                pasteboard.clearContents()
            }
        }
    }

    private func clearProxyToken() {
        isProxyTokenRevealed = false
        proxyTokenCopied = false
        model.clearOneTimeProxyToken()
    }

    private func saveConnection(_ draft: RoutingConnectionDraft) {
        localMutationFailure = nil
        guard let command = routingCommand(), draft.canSave else {
            localMutationFailure = AppLocalization.text("Routing target update unavailable")
            return
        }
        Task {
            await model.upsertConnection(
                draft.connection, secret: draft.secret, command: command
            )
        }
    }

    private func removeConnection(_ connectionRef: String) {
        localMutationFailure = nil
        guard let command = routingCommand() else {
            localMutationFailure = AppLocalization.text("Routing target update unavailable")
            return
        }
        Task {
            await model.removeConnection(connectionRef, command: command)
        }
    }

    private func saveTarget(_ draft: RoutingTargetDraft) {
        localMutationFailure = nil
        guard let command = routingCommand(),
              let value = draft.mutationValue(connections: model.connections)
        else {
            localMutationFailure = AppLocalization.text("Routing target update unavailable")
            return
        }
        Task { await model.upsertTarget(value, command: command) }
    }

    private func removeTarget(_ targetID: String) {
        localMutationFailure = nil
        guard let command = routingCommand() else {
            localMutationFailure = AppLocalization.text("Routing target update unavailable")
            return
        }
        Task { await model.removeTarget(targetID, command: command) }
    }

    private func savePolicy(_ draft: RoutingPolicyDraft) {
        localMutationFailure = nil
        guard let command = routingCommand(), let policy = draft.mutationValue else {
            localMutationFailure = AppLocalization.text("Routing target update unavailable")
            return
        }
        Task { await model.upsertCustomPolicy(policy, command: command) }
    }

    private func removePolicy(_ policyID: String) {
        localMutationFailure = nil
        guard let command = routingCommand() else {
            localMutationFailure = AppLocalization.text("Routing target update unavailable")
            return
        }
        Task { await model.removeCustomPolicy(policyID, command: command) }
    }

    private var decisionAPIStatus: String {
        guard model.health != nil else { return AppLocalization.text("Unavailable") }
        return AppLocalization.text(model.decisionAPIEnabled ? "Online" : "Disabled")
    }

    private func savePreferences(enabled: Bool, policyID: String) {
        localMutationFailure = nil
        guard let command = routingCommand() else {
            localMutationFailure = AppLocalization.text("Routing target update unavailable")
            return
        }
        Task {
            await model.savePreferences(
                decisionAPIEnabled: enabled,
                defaultPolicyID: policyID,
                command: command
            )
        }
    }

    private func importReplay(_ url: URL) {
        localEvaluationFailure = nil
        let scoped = url.startAccessingSecurityScopedResource()
        defer {
            if scoped { url.stopAccessingSecurityScopedResource() }
        }
        do {
            let values = try url.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey])
            guard values.isRegularFile == true,
                  let size = values.fileSize,
                  (1...65_536).contains(size)
            else {
                localEvaluationFailure = AppLocalization.text("Replay file must be a JSON file no larger than 64 KiB")
                return
            }
            let data = try Data(contentsOf: url, options: [.mappedIfSafe])
            Task { await model.replay(data) }
        } catch {
            localEvaluationFailure = AppLocalization.text("Replay file could not be read")
        }
    }

    private func routingCommand() -> ProviderMutationCommand? {
        guard let executable = Bundle.main.executableURL else { return nil }
        return ProviderMutationCommand.resolveRouting(
            activityBundleURL: Bundle.main.bundleURL,
            activityExecutableURL: executable
        )
    }

    private func proxyCommand(
        _ action: RoutingProxyAction
    ) -> ProviderMutationCommand? {
        guard let executable = Bundle.main.executableURL else { return nil }
        return ProviderMutationCommand.resolveProxy(
            action: action,
            activityBundleURL: Bundle.main.bundleURL,
            activityExecutableURL: executable
        )
    }
}
