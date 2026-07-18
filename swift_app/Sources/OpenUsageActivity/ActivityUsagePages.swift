import SwiftUI
import UsageCore

struct APISpendPage: View {
    let data: ActivityLoadedData

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            PageHeading("API Spend", detail: "Native currency values only")
            if data.apiSpend.totals.isEmpty {
                EmptyDataView(
                    title: data.apiSpend.coverage == .complete
                        ? "No spend reported"
                        : (data.apiSpend.coverage == .partial ? "Spend coverage incomplete" : "Spend unavailable"),
                    description: data.apiSpend.coverage == .complete
                        ? "Complete billing coverage reported no monetary charge for this selection."
                        : (data.apiSpend.coverage == .partial
                            ? "Some billing days or provider scopes have not reported a monetary value."
                            : "No authoritative or estimated API cost is present for this selection.")
                )
            } else {
                ForEach(data.apiSpend.totals, id: \.currency) { total in
                    HStack {
                        Label(total.quality.title, systemImage: total.quality.symbol)
                        Spacer()
                        Text(APISpendText.display(amount: total.amount, currency: total.currency)).monospacedDigit()
                    }
                    Divider()
                }
            }
        }
    }
}

struct LocalToolsPage: View {
    @Bindable var store: ActivityViewStore
    let data: ActivityLoadedData
    private var tools: [ProviderDisplayDescriptor] {
        data.visibleProviderIDs
            .filter { store.providerID == nil || store.providerID == $0 }
            .map(data.providerDescriptor)
            .filter { $0.category == .localTool }
    }
    private var summaries: [LocalToolUsageSummary] {
        LocalToolUsagePresentation.make(
            descriptors: tools, periodRecords: data.records,
            historyRecords: data.historyRecords, health: data.health.sources
        )
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            PageHeading(
                "Local Tools",
                detail: "Token activity from local runtime logs, separate from subscription quota"
            )
            if summaries.isEmpty {
                EmptyDataView(title: "No local tools", description: "Hermes and OpenClaw appear here when detected.")
            } else {
                ForEach(summaries) { summary in
                    VStack(alignment: .leading, spacing: 14) {
                        HStack {
                            Text(summary.displayName).font(.title3.weight(.semibold))
                            Text("Local runtime").font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            StateLabel(state: summary.state)
                        }
                        HStack(spacing: 36) {
                            LocalToolMetric(
                                value: summary.observedTokens > 0
                                    ? TokenText.compact(summary.observedTokens)
                                    : AppLocalization.text("No activity"),
                                label: AppLocalization.format("%@ Tokens", store.period.title)
                            )
                            LocalToolMetric(
                                value: "\(summary.activeDays)", label: "Active Days"
                            )
                            LocalToolMetric(
                                value: "\(summary.knownModelIDs.count)", label: "Known Models"
                            )
                            LocalToolMetric(
                                value: summary.lastActivityDay?.rawValue
                                    ?? AppLocalization.text("Unavailable"),
                                label: "Last Activity"
                            )
                        }
                        HStack(spacing: 12) {
                            Label(summary.quality.displayName, systemImage: summary.quality.symbol)
                            if !summary.knownModelIDs.isEmpty {
                                Text(summary.knownModelIDs.map(DisplayText.model).joined(separator: ", "))
                                    .lineLimit(1).truncationMode(.tail)
                            }
                            Spacer()
                            Text(summary.lastCollectionAt.map {
                                AppLocalization.format("Collected %@", DateText.display($0))
                            } ?? AppLocalization.text("Collection unavailable"))
                        }
                        .font(.caption).foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 4)
                    .accessibilityElement(children: .contain)
                    Divider()
                }
            }
        }
    }
}

private struct LocalToolMetric: View {
    let value: String
    let label: String

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(value).font(.headline).monospacedDigit()
            Text(AppLocalization.text(label)).font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
