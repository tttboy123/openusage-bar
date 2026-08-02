import SwiftUI
import UsageCore

struct RoutingPolicyEditor: View {
    @Environment(\.dismiss) private var dismiss
    @State private var draft: RoutingPolicyDraft
    @State private var confirmsRemoval = false
    let isSaving: Bool
    let onSave: (RoutingPolicyDraft) -> Void
    let onRemove: ((String) -> Void)?

    init(
        draft: RoutingPolicyDraft,
        isSaving: Bool,
        onSave: @escaping (RoutingPolicyDraft) -> Void,
        onRemove: ((String) -> Void)?
    ) {
        _draft = State(initialValue: draft)
        self.isSaving = isSaving
        self.onSave = onSave
        self.onRemove = onRemove
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            Form {
                identity
                weights
                safety
                advanced
                if !draft.isNew, onRemove != nil { removal }
            }
            .formStyle(.grouped)
        }
        .frame(minWidth: 680, minHeight: 720)
        .confirmationDialog(
            "Remove custom policy?",
            isPresented: $confirmsRemoval,
            titleVisibility: .visible
        ) {
            Button("Remove custom policy", role: .destructive) {
                onRemove?(draft.id)
                dismiss()
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Built-in policies remain available. Existing decision evidence is not deleted.")
        }
    }

    private var header: some View {
        HStack(spacing: 16) {
            VStack(alignment: .leading, spacing: 3) {
                Text(AppLocalization.text(
                    draft.isNew ? "Add custom policy" : "Edit custom policy"
                ))
                .font(.title2.weight(.semibold))
                Text("Hard safety filters always run before these scoring preferences")
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
    }

    private var identity: some View {
        Section {
            LabeledContent("Policy ID") {
                TextField("custom_coding", text: $draft.id)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 280)
                    .disabled(!draft.isNew)
            }
        } header: {
            Text("Policy identity")
        } footer: {
            Text("Use a stable public ID. Built-in policy IDs cannot be replaced.")
        }
    }

    private var weights: some View {
        Section {
            weightField("Reliability", value: $draft.reliabilityWeight)
            weightField("Headroom", value: $draft.headroomWeight)
            weightField("Latency", value: $draft.latencyWeight)
            weightField("Cost", value: $draft.costWeight)
            LabeledContent("Total") {
                Text("\(weightTotal)%")
                    .monospacedDigit()
                    .foregroundStyle(weightTotal == 100 ? .green : .orange)
            }
        } header: {
            Text("Scoring weights")
        } footer: {
            Text("Weights must total 100%. Hard filters cannot be weakened by a score.")
        }
    }

    private var safety: some View {
        Section("Safety thresholds") {
            integerField(
                "Minimum headroom (basis points)",
                value: $draft.minimumHeadroomBasisPoints
            )
            integerField(
                "Maximum error rate (basis points)",
                value: $draft.maximumErrorRateBasisPoints
            )
            int64Field("Minimum Runtime samples", value: $draft.minimumRuntimeSamples)
            Toggle("Require known cost", isOn: $draft.requireCost)
            Toggle("Require Runtime observations", isOn: $draft.requireRuntime)
        }
    }

    private var advanced: some View {
        Section {
            DisclosureGroup("Advanced references and classes") {
                int64Field(
                    "Latency reference (ms)",
                    value: $draft.latencyReferenceMilliseconds
                )
                int64Field(
                    "Cost reference (microunits)",
                    value: $draft.costReferenceMicrounits
                )
                LabeledContent("Comparison currency") {
                    TextField("USD", text: $draft.costCurrency)
                        .textFieldStyle(.roundedBorder)
                        .frame(maxWidth: 120)
                }
                int64Field(
                    "Minimum balance (microunits)",
                    value: $draft.minimumBalanceMicrounits
                )
                int64Field(
                    "Balance reference (microunits)",
                    value: $draft.balanceReferenceMicrounits
                )
                integerField(
                    "Unknown penalty (basis points)",
                    value: $draft.unknownPenaltyBasisPoints
                )
                classToggles
            }
        }
    }

    private var classToggles: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Allowed execution classes").font(.subheadline.weight(.medium))
            HStack(spacing: 12) {
                Toggle("Direct API", isOn: executionClass("direct_api"))
                Toggle("OpenAI-compatible", isOn: executionClass("openai_compatible"))
                Toggle("Subscription CLI", isOn: executionClass("subscription_cli"))
                Toggle("Self-hosted", isOn: executionClass("self_hosted"))
            }
            .toggleStyle(.checkbox)
            Text("Allowed privacy classes").font(.subheadline.weight(.medium))
            HStack(spacing: 12) {
                Toggle("Local only", isOn: privacyClass("local_only"))
                Toggle("Direct provider", isOn: privacyClass("direct_provider"))
                Toggle("Proxy allowed", isOn: privacyClass("proxy"))
            }
            .toggleStyle(.checkbox)
        }
        .padding(.vertical, 6)
    }

    private var removal: some View {
        Section {
            Button("Remove custom policy", role: .destructive) {
                confirmsRemoval = true
            }
            .disabled(isSaving)
        }
    }

    private var weightTotal: Int {
        draft.reliabilityWeight + draft.headroomWeight
            + draft.latencyWeight + draft.costWeight
    }

    private func weightField(_ title: LocalizedStringKey, value: Binding<Int>) -> some View {
        LabeledContent(title) {
            HStack(spacing: 6) {
                TextField(title, value: value, format: .number.grouping(.never))
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 80)
                Text("%").foregroundStyle(.secondary)
            }
        }
    }

    private func integerField(_ title: LocalizedStringKey, value: Binding<Int>) -> some View {
        LabeledContent(title) {
            TextField(title, value: value, format: .number.grouping(.never))
                .textFieldStyle(.roundedBorder)
                .frame(maxWidth: 180)
        }
    }

    private func int64Field(_ title: LocalizedStringKey, value: Binding<Int64>) -> some View {
        LabeledContent(title) {
            TextField(title, value: value, format: .number.grouping(.never))
                .textFieldStyle(.roundedBorder)
                .frame(maxWidth: 180)
        }
    }

    private func executionClass(_ value: String) -> Binding<Bool> {
        Binding(
            get: { draft.allowedExecutionClasses.contains(value) },
            set: { enabled in
                if enabled { draft.allowedExecutionClasses.insert(value) }
                else { draft.allowedExecutionClasses.remove(value) }
            }
        )
    }

    private func privacyClass(_ value: String) -> Binding<Bool> {
        Binding(
            get: { draft.allowedPrivacyClasses.contains(value) },
            set: { enabled in
                if enabled { draft.allowedPrivacyClasses.insert(value) }
                else { draft.allowedPrivacyClasses.remove(value) }
            }
        )
    }
}
