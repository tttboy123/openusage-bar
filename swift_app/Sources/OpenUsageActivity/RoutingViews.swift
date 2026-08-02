import SwiftUI
import UsageCore

struct RoutingPage: View {
    @State private var model = RoutingViewModel()
    @State private var localMutationFailure: String?
    @State private var editingConnection: RoutingConnectionDraft?
    @State private var editingTarget: RoutingTargetDraft?
    @State private var editingPolicy: RoutingPolicyDraft?

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
                    connections
                    targets
                    customPolicies
                    dryRun
                    decisionResult
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
                onRemove: draft.isNew ? nil : { policyID in
                    removePolicy(policyID)
                }
            )
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
                Label {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Decision API").font(.headline)
                        Text(model.health?.ok == true ? "Online" : "Unavailable")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                } icon: {
                    Image(systemName: model.health?.ok == true
                          ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                        .foregroundStyle(model.health?.ok == true ? .green : .orange)
                }
                Spacer()
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
        .accessibilityElement(children: .combine)
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
                    .disabled(model.isSimulating || model.policies.isEmpty)
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
            await model.loadConnections(command: command)
            await model.loadCustomPolicies(command: command)
        }
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

    private func routingCommand() -> ProviderMutationCommand? {
        guard let executable = Bundle.main.executableURL else { return nil }
        return ProviderMutationCommand.resolveRouting(
            activityBundleURL: Bundle.main.bundleURL,
            activityExecutableURL: executable
        )
    }
}
