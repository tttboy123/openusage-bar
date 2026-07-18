import SwiftUI
import UsageCore

struct OnboardingView: View {
    let phase: FirstRunPhase
    let primaryAction: () -> Void
    let skip: () -> Void

    var body: some View {
        VStack(spacing: 20) {
            Image(systemName: symbol)
                .font(.system(size: 34, weight: .medium))
                .foregroundStyle(.tint)
                .accessibilityHidden(true)
            VStack(spacing: 8) {
                Text(AppLocalization.text(titleKey))
                    .font(.title2.weight(.semibold))
                Text(AppLocalization.text(detailKey))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 460)
            }
            if case let .discoverableProviders(familyIDs) = phase {
                HStack(spacing: 8) {
                    ForEach(familyIDs, id: \.self) { familyID in
                        Text(ProviderCatalog.descriptor(for: familyID).displayName)
                            .font(.callout.weight(.medium))
                            .padding(.horizontal, 10).padding(.vertical, 6)
                            .background(.quaternary, in: Capsule())
                    }
                }
            }
            if phase == .collecting { ProgressView() }
            Button(AppLocalization.text(actionKey), action: primaryAction)
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
            Button(AppLocalization.text("Skip for now"), action: skip)
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
        }
        .padding(48)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(.background)
    }

    private var symbol: String {
        switch phase {
        case .discoverableProviders: "sparkle.magnifyingglass"
        case .needsConnection: "link.badge.plus"
        case .collecting: "arrow.triangle.2.circlepath"
        case .ready: "checkmark.circle"
        case .hidden: "chart.bar.xaxis"
        }
    }

    private var titleKey: String {
        switch phase {
        case .discoverableProviders: "Detected AI clients"
        case .needsConnection: "Connect your first provider"
        case .collecting: "Collecting your first metric"
        case .ready: "Your first metric is ready"
        case .hidden: "Getting Started"
        }
    }

    private var detailKey: String {
        switch phase {
        case .discoverableProviders: "Review the local clients OpenUsage Bar found on this Mac."
        case .needsConnection: "Add one provider connection to begin collecting trustworthy usage facts."
        case .collecting: "OpenUsage Bar is waiting for the first trustworthy usage or capacity fact."
        case .ready: "Your usage data is available in the native Activity dashboard."
        case .hidden: "OpenUsage Bar keeps usage facts local to this Mac."
        }
    }

    private var actionKey: String {
        switch phase {
        case .discoverableProviders: "Review Detected Providers"
        case .needsConnection: "Add Connection"
        case .collecting: "Refresh"
        case .ready: "View First Metric"
        case .hidden: "View Activity"
        }
    }
}
