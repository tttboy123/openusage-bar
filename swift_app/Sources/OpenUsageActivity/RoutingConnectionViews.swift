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
