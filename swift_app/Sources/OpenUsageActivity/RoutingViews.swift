import SwiftUI
import UsageCore

struct RoutingPage: View {
    @State private var model = RoutingViewModel()

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                header
                if let failure = model.failure, model.health == nil {
                    ContentUnavailableView(
                        failure.title,
                        systemImage: "arrow.triangle.branch",
                        description: Text("The local Decision API did not return verified routing data.")
                    )
                    .frame(maxWidth: .infinity, minHeight: 240)
                } else {
                    overview
                    targets
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
        .task { await model.load() }
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
                Task { await model.load() }
            }
            .disabled(model.isLoading)
            .keyboardShortcut("r", modifiers: [.command])
            .accessibilityHint("Reload routing health, policies, targets, and history")
        }
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
            sectionTitle("Routing targets", detail: "Only explicit, execution-capable targets are eligible")
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
        }
        .padding(.vertical, 12)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(target.modelID), \(DisplayText.provider(target.providerID)), \(readiness.title)")
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
}
