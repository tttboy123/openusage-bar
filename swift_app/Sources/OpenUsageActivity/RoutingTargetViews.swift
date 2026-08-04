import SwiftUI
import UsageCore

struct RoutingTargetEditor: View {
    @Environment(\.dismiss) private var dismiss
    @State private var draft: RoutingTargetDraft
    @State private var confirmsRemoval = false
    let connections: [RoutingExecutionConnection]
    let isSaving: Bool
    let onSave: (RoutingTargetDraft) -> Void
    let onRemove: ((String) -> Void)?

    init(
        draft: RoutingTargetDraft,
        connections: [RoutingExecutionConnection],
        isSaving: Bool,
        onSave: @escaping (RoutingTargetDraft) -> Void,
        onRemove: ((String) -> Void)?
    ) {
        _draft = State(initialValue: draft)
        self.connections = connections
        self.isSaving = isSaving
        self.onSave = onSave
        self.onRemove = onRemove
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            Form {
                executionSection
                resourceSection
                profileSection
                costSection
                if !draft.isNew, onRemove != nil { removalSection }
            }
            .formStyle(.grouped)
        }
        .frame(minWidth: 680, minHeight: 680)
        .confirmationDialog(
            "Remove routing target?",
            isPresented: $confirmsRemoval,
            titleVisibility: .visible
        ) {
            Button("Remove routing target", role: .destructive) {
                onRemove?(draft.id)
                dismiss()
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This stops new decisions from selecting the target. The execution connection and usage history remain available.")
        }
    }

    private var header: some View {
        HStack(spacing: 16) {
            VStack(alignment: .leading, spacing: 3) {
                Text(AppLocalization.text(
                    draft.isNew ? "Add routing target" : "Edit routing target"
                ))
                .font(.title2.weight(.semibold))
                Text("Bind one model to explicit, verified resource facts")
                    .font(.callout).foregroundStyle(.secondary)
            }
            Spacer()
            Button("Cancel") { dismiss() }
                .keyboardShortcut(.cancelAction)
            Button("Save") {
                onSave(draft)
                dismiss()
            }
            .buttonStyle(.borderedProminent)
            .keyboardShortcut(.defaultAction)
            .disabled(!draft.canSave(connections: connections) || isSaving)
        }
        .padding(20)
    }

    private var executionSection: some View {
        Section {
            Picker("Execution connection", selection: $draft.connectionRef) {
                ForEach(connections) { connection in
                    Text("\(DisplayText.provider(connection.providerID)) · \(connection.accountRef)")
                        .tag(connection.connectionRef)
                }
            }
            .onChange(of: draft.connectionRef) { _, value in
                guard let connection = connections.first(where: {
                    $0.connectionRef == value
                }) else { return }
                draft.selectConnection(connection)
            }

            Picker("Model", selection: $draft.modelID) {
                ForEach(selectedConnection?.models ?? [], id: \.self) { model in
                    Text(model).tag(model)
                }
            }
            Toggle("Eligible for routing", isOn: $draft.enabled)
        } header: {
            Text("Execution and model")
        } footer: {
            Text("Provider, account and adapter identity come from the selected execution connection and cannot drift independently.")
        }
    }

    private var resourceSection: some View {
        Section {
            Picker("Resource mode", selection: $draft.resourceMode) {
                Text("Subscription quota").tag("quota")
                Text("API balance").tag("balance")
            }
            .pickerStyle(.segmented)

            LabeledContent("Usage account reference") {
                TextField("account-1", text: $draft.factAccountRef)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 300)
            }
            if draft.resourceMode == "balance" {
                LabeledContent("Balance currency") {
                    TextField("USD", text: $draft.balanceCurrency)
                        .textFieldStyle(.roundedBorder)
                        .frame(maxWidth: 140)
                }
            }
            LabeledContent("Runtime scope (optional)") {
                TextField("anon_…", text: $draft.runtimeScopeRef)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 300)
            }
        } header: {
            Text("Resource facts")
        } footer: {
            Text("The usage account reference must match one local capacity or balance fact. Leaving it blank keeps the resource state Unknown, never zero.")
        }
    }

    private var profileSection: some View {
        Section("Model profile") {
            LabeledContent("Capabilities") {
                HStack(spacing: 14) {
                    Toggle("Chat", isOn: capability("chat"))
                    Toggle("Reasoning", isOn: capability("reasoning"))
                    Toggle("Tools", isOn: capability("tools"))
                }
                .toggleStyle(.checkbox)
            }
            LabeledContent("Context window") {
                TextField(
                    "Context window",
                    value: $draft.contextWindowTokens,
                    format: .number.grouping(.never)
                )
                .textFieldStyle(.roundedBorder)
                .frame(maxWidth: 180)
            }
            Picker("Quality tier", selection: $draft.qualityTier) {
                ForEach(0 ... 5, id: \.self) { value in
                    Text("\(value)").tag(value)
                }
            }
            Picker("Privacy class", selection: $draft.privacyClass) {
                Text("Local only").tag("local_only")
                Text("Direct provider").tag("direct_provider")
                Text("Proxy allowed").tag("proxy")
            }
            LabeledContent("Regions") {
                TextField("global", text: $draft.regionsText)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 300)
            }
        }
    }

    private var costSection: some View {
        Section {
            LabeledContent("Currency") {
                TextField("USD", text: $draft.costCurrency)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 140)
            }
            LabeledContent("Input cost per 1M Token") {
                TextField("3.25", text: $draft.inputCostPerMillion)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 180)
            }
            LabeledContent("Output cost per 1M Token") {
                TextField("15", text: $draft.outputCostPerMillion)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 180)
            }
        } header: {
            Text("Optional cost metadata")
        } footer: {
            Text("Provide all three values or leave all three blank. Balance routes require matching balance and cost currencies.")
        }
    }

    private var removalSection: some View {
        Section {
            Button("Remove routing target", role: .destructive) {
                confirmsRemoval = true
            }
            .disabled(isSaving)
        }
    }

    private var selectedConnection: RoutingExecutionConnection? {
        connections.first { $0.connectionRef == draft.connectionRef }
    }

    private func capability(_ value: String) -> Binding<Bool> {
        Binding(
            get: { draft.capabilities.contains(value) },
            set: { enabled in
                if enabled { draft.capabilities.insert(value) }
                else { draft.capabilities.remove(value) }
            }
        )
    }
}
