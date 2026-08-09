import SwiftUI
import UsageCore

struct StatusLabelView: View {
    let label: StatusLabel
    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: "chart.bar.xaxis")
            ForEach(label.values, id: \.self) { Text($0).monospacedDigit() }
        }
    }
}

struct MenuBarPopover: View {
    @Bindable var model: MenuBarViewModel

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            Button {
                HelperLauncher.openActivity(route: "health")
            } label: {
                Label(
                    AppLocalization.text("Open Main Window"),
                    systemImage: "macwindow"
                )
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            Divider()
            today
            capacityHeader
            ScrollView {
                LazyVStack(spacing: 0) {
                    if model.groups.isEmpty { emptyState } else { ForEach(model.groups) { provider($0) } }
                }
            }
            .frame(minHeight: 120, maxHeight: 330)
            Button("View all providers") {
                HelperLauncher.openActivity(route: MenuDestination.allProviders.transportValue)
            }
                .buttonStyle(.plain).foregroundStyle(.tint).padding(.vertical, 10)
            Divider()
            footer
        }
        .frame(width: 400)
        .background(.regularMaterial)
        .task { model.loadLastGoodOnce(); model.checkFreshness() }
        .onMoveCommand { direction in
            if direction == .down { model.perform(.moveSelection(1)) }
            if direction == .up { model.perform(.moveSelection(-1)) }
        }
        .onKeyPress(keys: [.return, .space, .escape]) { press in
            if press.key == .escape { model.perform(MenuKeyRouter.action(for: .escape, hasExpansion: model.expandedProviderID != nil)) }
            else { model.perform(.activateSelection) }
            return .handled
        }
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 3) {
                Text(AppLocalization.text("UsageHub")).font(.headline)
                Text(model.updatedAge).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            UnifiedRefreshButton(isRefreshing: model.isRefreshing, action: model.refresh)
                .keyboardShortcut("r", modifiers: .command)
        }
        .padding(.horizontal, 16).padding(.vertical, 13)
    }

    private var today: some View {
        HStack {
            Text("Today Token").foregroundStyle(.secondary)
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(model.todayPresentation.value)
                    .font(.title3.weight(.semibold)).monospacedDigit()
                    .foregroundStyle(model.summary?.todayTokens == nil ? .secondary : .primary)
                if let coverage = model.todayPresentation.coverage {
                    Label(coverage, systemImage: "circle.lefthalf.filled")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 14)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Today Token")
        .accessibilityValue(model.todayPresentation.accessibilityValue)
    }

    private var capacityHeader: some View {
        HStack {
            Text("Capacity").font(.subheadline.weight(.semibold))
            Spacer()
            Text("Most urgent first").font(.caption).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 16).padding(.top, 6).padding(.bottom, 4)
    }

    private func provider(_ group: ProviderCapacityGroup) -> some View {
        let item = ProviderRowPresentation(group.primary)
        let descriptor = item.providerDescriptor
        let urls = providerURLs(for: descriptor.familyID)
        return VStack(spacing: 0) {
            Button { model.selectedProviderID = group.id; model.toggle(group.id) } label: {
                HStack(spacing: 12) {
                    UnifiedProviderAvatar(familyID: descriptor.familyID, displayName: item.provider, size: 36)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.provider).font(.body.weight(.medium)).lineLimit(1)
                        Text(item.window).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    UnifiedStatusBadge(status: badgeStatus(for: item), title: badgeTitle(for: item))
                    if !group.secondary.isEmpty {
                        Image(systemName: model.expandedProviderID == group.id ? "chevron.up" : "chevron.down")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    ProviderHoverActions(consoleURL: urls.console, credentialURL: urls.credential)
                }
                .contentShape(Rectangle())
                .padding(.horizontal, 14)
                .padding(.vertical, 9)
            }
            .buttonStyle(MenuProviderRowStyle(isSelected: model.selectedProviderID == group.id))
            .accessibilityLabel(item.accessibilityLabel).accessibilityValue(item.accessibilityValue)
            if model.expandedProviderID == group.id { ForEach(Array(group.secondary), id: \.recordID) { secondary($0) } }
        }
    }

    private func secondary(_ row: CapacityItem) -> some View {
        let item = ProviderRowPresentation(row)
        return HStack {
            Text(item.window).foregroundStyle(.secondary)
            Spacer()
            UnifiedStatusBadge(status: badgeStatus(for: item), title: badgeTitle(for: item))
        }
        .font(.caption).padding(.leading, 62).padding(.trailing, 16).padding(.vertical, 5)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(item.accessibilityLabel)
        .accessibilityValue(item.accessibilityValue)
    }

    private func badgeStatus(for item: ProviderRowPresentation) -> UnifiedStatusBadge.Status {
        if item.isCritical { return .critical }
        if item.isWarning { return .warning }
        return item.capacity == AppLocalization.text("Unavailable") ? .neutral : .ok
    }

    private func badgeTitle(for item: ProviderRowPresentation) -> String {
        item.capacity
    }

    private func providerURLs(for familyID: String) -> (console: URL?, credential: URL?) {
        let console = GeneratedProviderCatalog.quickConnectURLs[familyID].flatMap(URL.init(string:))
        let credential = GeneratedProviderCatalog.apiKeyURLs[familyID].flatMap(URL.init(string:))
        return (console, credential)
    }

    private var emptyState: some View {
        let presentation = model.emptyStatePresentation
        return UnifiedEmptyState(
            symbol: "tray",
            title: AppLocalization.text(presentation.titleKey),
            body: model.displayError ?? AppLocalization.text(presentation.detailKey),
            actionTitle: AppLocalization.text(presentation.actionKey),
            action: { HelperLauncher.openActivity(route: presentation.primaryRoute.transportValue) }
        )
    }

    private var footer: some View {
        HStack(spacing: 14) {
            Button("Open Usage Details") { HelperLauncher.openActivity() }.keyboardShortcut("d", modifiers: .command)
            Spacer()
            if model.hasHealthIssues { Button("Data Health") { HelperLauncher.openHealth() } }
            Button("Settings") { HelperLauncher.openSettings() }.keyboardShortcut(",", modifiers: .command)
        }
        .buttonStyle(.plain).font(.caption).padding(.horizontal, 16).padding(.vertical, 11)
    }
}

private struct MenuProviderRowStyle: ButtonStyle {
    let isSelected: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(isSelected ? DesignTokens.accent.opacity(0.10) : Color.clear, in: RoundedRectangle(cornerRadius: 10))
            .overlay {
                if isSelected || configuration.isPressed {
                    RoundedRectangle(cornerRadius: 10)
                        .stroke(DesignTokens.accent.opacity(configuration.isPressed ? 0.5 : 0.3), lineWidth: 1)
                }
            }
            .scaleEffect(configuration.isPressed ? 0.995 : 1)
    }
}

private struct ProviderHoverActions: View {
    let consoleURL: URL?
    let credentialURL: URL?

    var body: some View {
        HStack(spacing: 6) {
            if let consoleURL {
                Link(destination: consoleURL) {
                    Image(systemName: "arrow.up.forward.square")
                        .imageScale(.small)
                        .foregroundStyle(.secondary)
                }
                .help(AppLocalization.text("Open Console"))
            }
            if let credentialURL {
                Link(destination: credentialURL) {
                    Image(systemName: "key")
                        .imageScale(.small)
                        .foregroundStyle(.secondary)
                }
                .help(AppLocalization.text("Get API Key"))
            }
        }
    }
}
