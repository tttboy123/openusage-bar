import SwiftUI
import UsageCore

struct RoutingConnectionEditor: View {
    @Environment(\.dismiss) private var dismiss
    @State private var draft: RoutingConnectionDraft
    let isSaving: Bool
    let onSave: (RoutingConnectionDraft) -> Void
    let onRemove: ((String) -> Void)?

    init(
        draft: RoutingConnectionDraft,
        isSaving: Bool,
        onSave: @escaping (RoutingConnectionDraft) -> Void,
        onRemove: ((String) -> Void)?
    ) {
        _draft = State(initialValue: draft)
        self.isSaving = isSaving
        self.onSave = onSave
        self.onRemove = onRemove
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text(AppLocalization.text(
                        draft.isNew
                            ? "Add execution connection"
                            : "Edit execution connection"
                    ))
                        .font(.title2.weight(.semibold))
                    Text("OpenAI-compatible HTTPS endpoint")
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
                .disabled(!draft.canSave || isSaving)
            }
            .padding(20)
            Divider()
            Form {
                Section("Connection") {
                    LabeledContent("Provider ID") {
                        TextField("glm", text: $draft.providerID)
                            .textFieldStyle(.roundedBorder)
                            .frame(maxWidth: 300)
                    }
                    LabeledContent("Account reference") {
                        TextField("account-1", text: $draft.accountRef)
                            .textFieldStyle(.roundedBorder)
                            .frame(maxWidth: 300)
                    }
                    LabeledContent("Base URL") {
                        TextField("https://api.example.com/v1", text: $draft.baseURL)
                            .textFieldStyle(.roundedBorder)
                            .frame(maxWidth: 380)
                    }
                    LabeledContent("Models") {
                        TextField("model-a, model-b", text: $draft.modelsText)
                            .textFieldStyle(.roundedBorder)
                            .frame(maxWidth: 380)
                    }
                    Toggle("Eligible for routing", isOn: $draft.enabled)
                }
                Section("Credential") {
                    SecureField(AppLocalization.text(
                        draft.isNew
                            ? "Inference API key"
                            : "Leave blank to keep the current key"
                    ), text: $draft.secret)
                    Text("Stored in Keychain. The value is never displayed again or written to routing configuration.")
                        .font(.caption).foregroundStyle(.secondary)
                    Text("Subscription and billing keys are not assumed to be inference credentials.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if !draft.isNew, let onRemove {
                    Section {
                        Button("Remove connection", role: .destructive) {
                            onRemove(draft.id)
                            dismiss()
                        }
                        .disabled(isSaving)
                    }
                }
            }
            .formStyle(.grouped)
        }
        .frame(minWidth: 620, minHeight: 540)
    }
}

struct RoutingProviderExecutionImportEditor: View {
    @Environment(\.dismiss) private var dismiss
    @State private var draft: RoutingProviderExecutionImportDraft
    let isSaving: Bool
    let onImport: (RoutingProviderExecutionImportDraft) -> Void

    init(
        draft: RoutingProviderExecutionImportDraft,
        isSaving: Bool,
        onImport: @escaping (RoutingProviderExecutionImportDraft) -> Void
    ) {
        _draft = State(initialValue: draft)
        self.isSaving = isSaving
        self.onImport = onImport
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 16) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Reuse Provider credential")
                        .font(.title2.weight(.semibold))
                    Text("Create an isolated execution connection without displaying the key")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Import") {
                    onImport(draft)
                    dismiss()
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(!draft.canImport || isSaving)
            }
            .padding(20)
            Divider()
            Form {
                Section("Provider Center connection") {
                    LabeledContent("Provider") {
                        Text(draft.template.displayName)
                    }
                    LabeledContent("Region") {
                        Text(draft.template.site == "china" ? "China" : "International")
                    }
                    LabeledContent("Fixed endpoint") {
                        Text(draft.template.baseURL)
                            .font(.callout.monospaced())
                            .textSelection(.enabled)
                    }
                    LabeledContent("Models") {
                        TextField("model-a, model-b", text: $draft.modelsText)
                            .textFieldStyle(.roundedBorder)
                            .frame(maxWidth: 380)
                    }
                }
                Section("Authorization") {
                    Toggle(
                        "Copy this Provider credential into an isolated routing Keychain item",
                        isOn: $draft.confirmsLocalCopy
                    )
                    Text("The key stays on this Mac. It is not returned to the app UI, command output, logs, or routing configuration.")
                        .font(.caption).foregroundStyle(.secondary)
                    if !draft.template.credentialAvailable {
                        Label(
                            "No readable inference credential is saved for this connection",
                            systemImage: "key.slash"
                        )
                        .foregroundStyle(.orange)
                    }
                    if ["minimax", "step_plan"].contains(draft.template.familyID) {
                        Toggle(
                            "Create routing targets for the selected models",
                            isOn: $draft.createsRoutingTargets
                        )
                        Text("Generated targets use conservative defaults and can be edited before enabling the local proxy.")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        Text("This Provider uses balance facts. Add routing targets separately so cost and currency remain explicit.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            .formStyle(.grouped)
        }
        .frame(minWidth: 650, minHeight: 500)
    }
}
